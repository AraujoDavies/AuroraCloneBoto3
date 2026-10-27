"""
Aurora Clone — versão pública.

Pipeline: clone PROD→TEMP, dump DEV, restore→TEMP, validate,
rename DEV→OLD / TEMP→DEV, delete OLD.
Pré-voo: inventário RDS + policy IAM AvoidDeleteAuroraCloneBoto3.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import boto3
import pymysql
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# override=True: após trocar a secret key, o arquivo vence variáveis antigas do shell.
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s - %(levelname)s: %(message)s",
)

AVOID_DELETE_POLICY_NAME = "AvoidDeleteAuroraCloneBoto3"
POLL_INTERVAL_SECONDS = 30
SYSTEM_SCHEMAS = frozenset(
    {"mysql", "information_schema", "performance_schema", "sys"}
)


class AuroraClone:
    """Orquestra o clone Aurora PROD → DEV (passos 1–7)."""

    def __init__(self, region_name: str | None = None):
        region = region_name or os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION")
        try:
            self.rds = (
                boto3.client("rds", region_name=region)
                if region
                else boto3.client("rds")
            )
        except Exception as error:
            raise RuntimeError(f"boto3 RDS Failed: {error}") from error
        try:
            self.iam = boto3.client("iam")
            self.sts = boto3.client("sts")
        except Exception as error:
            raise RuntimeError(f"boto3 IAM/STS Failed: {error}") from error
        try:
            self.ssm = (
                boto3.client("ssm", region_name=region)
                if region
                else boto3.client("ssm")
            )
        except Exception as error:
            raise RuntimeError(f"boto3 SSM Failed: {error}") from error
        self.region = self.rds.meta.region_name

        self.prod_id = (os.getenv("PROD_IDENTIFIER") or "").strip()
        self.dev_id = (os.getenv("DEV_IDENTIFIER") or "").strip()
        self.temp_id = (os.getenv("TEMP_IDENTIFIER") or "").strip()
        self.old_id = (os.getenv("OLD_IDENTIFIER") or "").strip()

        self.db_subnet_group = (os.getenv("DB_SUBNET_GROUP") or "").strip()
        self.vpc_security_group_ids = [
            sg.strip()
            for sg in (os.getenv("VPC_SECURITY_GROUP_IDS") or "").split(",")
            if sg.strip()
        ]
        self.db_instance_class = (
            os.getenv("DB_INSTANCE_CLASS") or "db.serverless"
        ).strip()
        self.engine = (os.getenv("ENGINE") or "aurora-mysql").strip()

        self.db_user = (os.getenv("DB_USER") or "").strip()
        self.db_password = os.getenv("DB_PASSWORD") or ""
        self.db_password_ssm_param = (
            os.getenv("DB_PASSWORD_SSM_PARAM") or ""
        ).strip()
        self._db_credentials_resolved = False

        self.dev_schemas = [
            s.strip()
            for s in (os.getenv("DEV_SCHEMAS") or "").split(",")
            if s.strip()
        ]
        self.dump_base_dir = Path(
            (os.getenv("DUMP_BASE_DIR") or "./dumps").strip()
        )
        self.row_diff_tolerance = int(os.getenv("ROW_DIFF_TOLERANCE") or "50")
        self.wait_timeout_seconds = int(
            os.getenv("WAIT_TIMEOUT_SECONDS") or "3600"
        )
        self.keep_old_dev_hours = float(os.getenv("KEEP_OLD_DEV_HOURS") or "0")

    # ------------------------------------------------------------------
    # Config / guards / wait helpers
    # ------------------------------------------------------------------

    def _require_env(self, *keys: str) -> None:
        """Falha cedo se faltar variável obrigatória (atributo ou env)."""
        attr_map = {
            "PROD_IDENTIFIER": self.prod_id,
            "DEV_IDENTIFIER": self.dev_id,
            "TEMP_IDENTIFIER": self.temp_id,
            "OLD_IDENTIFIER": self.old_id,
            "DB_SUBNET_GROUP": self.db_subnet_group,
            "VPC_SECURITY_GROUP_IDS": ",".join(self.vpc_security_group_ids),
            "DB_USER": self.db_user,
            "DB_PASSWORD": self.db_password,
            "DEV_SCHEMAS": ",".join(self.dev_schemas),
        }
        missing = []
        for key in keys:
            value = attr_map.get(key)
            if value is None:
                value = (os.getenv(key) or "").strip()
            if not value:
                missing.append(key)
        if missing:
            raise RuntimeError(
                f"Variáveis obrigatórias ausentes: {', '.join(missing)}"
            )

    def _resolve_db_password(self) -> None:
        """DB_PASSWORD do env; se vazio, SecureString em DB_PASSWORD_SSM_PARAM."""
        if self.db_password:
            logging.info("DB_PASSWORD resolvida (fonte=env).")
            return
        param = self.db_password_ssm_param or (
            os.getenv("DB_PASSWORD_SSM_PARAM") or ""
        ).strip()
        if not param:
            raise RuntimeError(
                "DB_PASSWORD ausente: defina DB_PASSWORD no env ou "
                "DB_PASSWORD_SSM_PARAM com o path de um SecureString no SSM."
            )
        try:
            response = self.ssm.get_parameter(Name=param, WithDecryption=True)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "ClientError")
            raise RuntimeError(
                f"Falha ao ler DB_PASSWORD do SSM ({param}): {code}"
            ) from error
        value = (response.get("Parameter") or {}).get("Value") or ""
        if not value:
            raise RuntimeError(f"Parâmetro SSM {param} existe mas está vazio.")
        self.db_password = value
        logging.info("DB_PASSWORD resolvida (fonte=ssm, param=%s).", param)

    def _resolve_db_user(self) -> None:
        """
        DB_USER do env, ou default ao master se DEV==PROD.
        Se masters diferem e DB_USER vazio → erro cedo.
        """
        self._require_env("PROD_IDENTIFIER", "DEV_IDENTIFIER")
        master_prod = self._cluster_master_username(self.prod_id)
        master_dev = self._cluster_master_username(self.dev_id)
        logging.info(
            "MasterUsername PROD=%s DEV=%s",
            master_prod,
            master_dev,
        )
        if self.db_user:
            if not re.fullmatch(r"[A-Za-z0-9_]+", self.db_user):
                raise ValueError(
                    f"DB_USER inválido ({self.db_user!r}): use só letras, "
                    "números e underscore."
                )
            logging.info("DB_USER=%s (fonte=env).", self.db_user)
            return
        if master_dev == master_prod:
            self.db_user = master_prod
            logging.info(
                "DB_USER omitido; masters DEV/PROD iguais, usando %s.",
                self.db_user,
            )
            return
        raise RuntimeError(
            "DB_USER é obrigatório: MasterUsername DEV "
            f"({master_dev}) ≠ PROD ({master_prod})."
        )

    def _resolve_db_credentials(self) -> None:
        """Resolve senha + user no início do passo MySQL/pipeline (idempotente)."""
        if self._db_credentials_resolved:
            return
        self._resolve_db_password()
        self._resolve_db_user()
        self._db_credentials_resolved = True

    def _assert_not_prod(self, identifier: str, action: str) -> None:
        """Bloqueia rename/delete se o alvo for PROD."""
        if not identifier:
            raise RuntimeError(f"{action}: identifier vazio.")
        if self.prod_id and identifier == self.prod_id:
            raise RuntimeError(
                f"Recusado: {action} em PROD_IDENTIFIER={identifier}."
            )

    def _cluster_exists(self, identifier: str) -> bool:
        try:
            self.rds.describe_db_clusters(DBClusterIdentifier=identifier)
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] == "DBClusterNotFoundFault":
                return False
            raise

    def _instance_exists(self, identifier: str) -> bool:
        try:
            self.rds.describe_db_instances(DBInstanceIdentifier=identifier)
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] == "DBInstanceNotFound":
                return False
            raise

    def _wait_cluster(self, identifier: str, desired: str = "available") -> None:
        """Aguarda status do cluster. NotFound (ex.: pós-rename) é retentado."""
        logging.info(
            "Aguardando cluster %s → %s (timeout=%ss)...",
            identifier,
            desired,
            self.wait_timeout_seconds,
        )
        deadline = time.monotonic() + self.wait_timeout_seconds
        while time.monotonic() < deadline:
            try:
                response = self.rds.describe_db_clusters(
                    DBClusterIdentifier=identifier
                )
            except ClientError as error:
                if error.response["Error"]["Code"] == "DBClusterNotFoundFault":
                    logging.info(
                        "Cluster %s ainda não visível (rename em andamento?)...",
                        identifier,
                    )
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                raise
            status = response["DBClusters"][0].get("Status")
            logging.info("Cluster %s status=%s", identifier, status)
            if status == desired:
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Timeout aguardando cluster {identifier} → {desired}"
        )

    def _wait_instance(self, identifier: str, desired: str = "available") -> None:
        """Aguarda status da instância. NotFound (ex.: pós-rename) é retentado."""
        logging.info(
            "Aguardando instância %s → %s (timeout=%ss)...",
            identifier,
            desired,
            self.wait_timeout_seconds,
        )
        deadline = time.monotonic() + self.wait_timeout_seconds
        while time.monotonic() < deadline:
            try:
                response = self.rds.describe_db_instances(
                    DBInstanceIdentifier=identifier
                )
            except ClientError as error:
                if error.response["Error"]["Code"] == "DBInstanceNotFound":
                    logging.info(
                        "Instância %s ainda não visível (rename em andamento?)...",
                        identifier,
                    )
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                raise
            status = response["DBInstances"][0].get("DBInstanceStatus")
            logging.info("Instância %s status=%s", identifier, status)
            if status == desired:
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Timeout aguardando instância {identifier} → {desired}"
        )

    def _cluster_endpoint(self, identifier: str) -> str:
        response = self.rds.describe_db_clusters(DBClusterIdentifier=identifier)
        endpoint = response["DBClusters"][0].get("Endpoint")
        if not endpoint:
            raise RuntimeError(f"Cluster {identifier} sem Endpoint writer.")
        return endpoint

    def _cluster_master_username(self, identifier: str) -> str:
        response = self.rds.describe_db_clusters(DBClusterIdentifier=identifier)
        username = (response["DBClusters"][0].get("MasterUsername") or "").strip()
        if not username:
            raise RuntimeError(f"Cluster {identifier} sem MasterUsername.")
        return username

    def _dump_dir_today(self) -> Path:
        dated = datetime.now().strftime("%d%m%Y")
        path = self.dump_base_dir / dated
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _mysql_connect(
        self,
        host: str,
        user: str | None = None,
        password: str | None = None,
    ):
        return pymysql.connect(
            host=host,
            user=user if user is not None else self.db_user,
            password=password if password is not None else self.db_password,
            connect_timeout=30,
            read_timeout=120,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _reset_temp_master_password(self) -> None:
        """Redefine MasterUserPassword do TEMP para DB_PASSWORD (API RDS)."""
        self._require_env("TEMP_IDENTIFIER", "DB_PASSWORD")
        logging.info(
            "Redefinindo MasterUserPassword do TEMP %s (DB_PASSWORD)...",
            self.temp_id,
        )
        self.rds.modify_db_cluster(
            DBClusterIdentifier=self.temp_id,
            MasterUserPassword=self.db_password,
            ApplyImmediately=True,
        )
        self._wait_cluster(self.temp_id, "available")

    def _ensure_temp_db_user(self, master_username: str) -> None:
        """
        Se DB_USER ≠ MasterUsername do TEMP, cria/atualiza o user operacional
        com DB_PASSWORD e grants. Se iguais, só confirma login.
        """
        self._require_env("TEMP_IDENTIFIER", "DB_PASSWORD")
        if not self.db_user:
            raise RuntimeError(
                "DB_USER ausente: chame _resolve_db_credentials antes."
            )
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.db_user):
            raise ValueError(
                f"DB_USER inválido ({self.db_user!r}): use só letras, "
                "números e underscore."
            )

        endpoint = self._cluster_endpoint(self.temp_id)
        if self.db_user == master_username:
            logging.info(
                "DB_USER=%s é o MasterUsername do TEMP; senha já alinhada.",
                self.db_user,
            )
            with self._mysql_connect(endpoint):
                pass
            logging.info("Smoke login TEMP ok (master=%s).", self.db_user)
            return

        logging.info(
            "DB_USER=%s ≠ MasterUsername=%s; criando/atualizando user no TEMP...",
            self.db_user,
            master_username,
        )
        with self._mysql_connect(
            endpoint, user=master_username, password=self.db_password
        ) as conn:
            # Username já validado (A-Za-z0-9_); senha escapada pela conexão.
            quoted_password = conn.escape(self.db_password)
            user = self.db_user
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE USER IF NOT EXISTS '{user}'@'%' "
                    f"IDENTIFIED BY {quoted_password}"
                )
                cur.execute(
                    f"ALTER USER '{user}'@'%' IDENTIFIED BY {quoted_password}"
                )
                cur.execute(
                    f"GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%' "
                    "WITH GRANT OPTION"
                )
                cur.execute("FLUSH PRIVILEGES")
            conn.commit()

        with self._mysql_connect(endpoint):
            pass
        logging.info("Smoke login TEMP ok (DB_USER=%s).", self.db_user)

    def _list_user_schemas(self, host: str) -> list[str]:
        """Lista schemas de usuário em host (exclui SYSTEM_SCHEMAS)."""
        with self._mysql_connect(host) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT SCHEMA_NAME AS schema_name
                    FROM information_schema.SCHEMATA
                    ORDER BY SCHEMA_NAME
                    """
                )
                names = [row["schema_name"] for row in cur.fetchall()]
        return sorted(s for s in names if s not in SYSTEM_SCHEMAS)

    def _resolve_dev_only_schemas(self) -> list[str]:
        """
        Schemas a dump/restore (não validate).
        Override: DEV_SCHEMAS se setado.
        Auto: schemas(DEV) − schemas(TEMP) (TEMP espelha PROD pós-clone).
        """
        if self.dev_schemas:
            logging.info(
                "DEV_SCHEMAS override (%s): %s",
                len(self.dev_schemas),
                self.dev_schemas,
            )
            return list(self.dev_schemas)

        if not self.temp_id:
            raise RuntimeError(
                "TEMP_IDENTIFIER ausente: necessário para auto-descobrir "
                "schemas (DEV − TEMP). Rode make clone-prod-to-temp antes "
                "ou defina DEV_SCHEMAS."
            )

        try:
            dev_host = self._cluster_endpoint(self.dev_id)
            temp_host = self._cluster_endpoint(self.temp_id)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code == "DBClusterNotFoundFault":
                raise RuntimeError(
                    "Cluster TEMP (ou DEV) não encontrado. Rode "
                    "make clone-prod-to-temp antes do dump, ou defina "
                    "DEV_SCHEMAS."
                ) from error
            raise

        try:
            dev_schemas = self._list_user_schemas(dev_host)
            temp_schemas = self._list_user_schemas(temp_host)
        except Exception as error:
            raise RuntimeError(
                "Falha ao listar schemas em DEV/TEMP. Confirme que TEMP "
                "está available e acessível com DB_USER/DB_PASSWORD, ou "
                f"defina DEV_SCHEMAS. Detalhe: {error}"
            ) from error

        only_dev = sorted(set(dev_schemas) - set(temp_schemas))
        logging.info(
            "Auto schemas DEV−TEMP: dev=%s temp=%s only_dev=%s → %s",
            len(dev_schemas),
            len(temp_schemas),
            len(only_dev),
            only_dev,
        )
        if not only_dev:
            raise RuntimeError(
                "Nenhum schema exclusivo de DEV (DEV − TEMP vazio). "
                "Confira os clusters ou defina DEV_SCHEMAS."
            )
        return only_dev

    def _resolve_restored_schemas(self) -> list[str]:
        """
        Schemas a validar (pós-restore): dump do dia ou DEV_SCHEMAS.
        Não usa DEV − TEMP (após myloader a diferença fica vazia).
        """
        if self.dev_schemas:
            logging.info(
                "DEV_SCHEMAS override (%s): %s",
                len(self.dev_schemas),
                self.dev_schemas,
            )
            return list(self.dev_schemas)

        dump_dir = self._dump_dir_today()
        schemas = sorted(
            p.name
            for p in dump_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
        logging.info(
            "Schemas do dump %s (%s): %s",
            dump_dir,
            len(schemas),
            schemas,
        )
        if not schemas:
            raise RuntimeError(
                f"Nenhum schema na pasta de dump {dump_dir}. "
                "Rode dump+restore antes do validate, ou defina DEV_SCHEMAS."
            )
        return schemas

    def _table_row_counts(self, host: str, schemas: list[str]) -> dict[str, int]:
        """Retorna mapa schema.table → COUNT(*)."""
        counts: dict[str, int] = {}
        with self._mysql_connect(host) as conn:
            with conn.cursor() as cur:
                for schema in schemas:
                    cur.execute(
                        """
                        SELECT TABLE_NAME AS table_name
                        FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                        ORDER BY TABLE_NAME
                        """,
                        (schema,),
                    )
                    tables = [row["table_name"] for row in cur.fetchall()]
                    for table in tables:
                        key = f"{schema}.{table}"
                        cur.execute(
                            f"SELECT COUNT(*) AS cnt FROM `{schema}`.`{table}`"
                        )
                        counts[key] = int(cur.fetchone()["cnt"])
        return counts

    def _delete_cluster_and_instance(self, identifier: str) -> None:
        """Apaga writer + cluster (skip final snapshot). Nunca PROD."""
        self._assert_not_prod(identifier, "delete")
        if self._instance_exists(identifier):
            logging.info("Deletando instância %s...", identifier)
            self.rds.delete_db_instance(
                DBInstanceIdentifier=identifier,
                SkipFinalSnapshot=True,
                DeleteAutomatedBackups=True,
            )
            deadline = time.monotonic() + self.wait_timeout_seconds
            while time.monotonic() < deadline and self._instance_exists(
                identifier
            ):
                logging.info("Aguardando remoção da instância %s...", identifier)
                time.sleep(POLL_INTERVAL_SECONDS)
            if self._instance_exists(identifier):
                raise TimeoutError(
                    f"Timeout removendo instância {identifier}"
                )

        if self._cluster_exists(identifier):
            logging.info("Deletando cluster %s...", identifier)
            self.rds.delete_db_cluster(
                DBClusterIdentifier=identifier,
                SkipFinalSnapshot=True,
            )
            deadline = time.monotonic() + self.wait_timeout_seconds
            while time.monotonic() < deadline and self._cluster_exists(
                identifier
            ):
                logging.info("Aguardando remoção do cluster %s...", identifier)
                time.sleep(POLL_INTERVAL_SECONDS)
            if self._cluster_exists(identifier):
                raise TimeoutError(f"Timeout removendo cluster {identifier}")

    def _rename_cluster_and_instance(
        self, from_id: str, to_id: str
    ) -> None:
        """Renomeia cluster e writer (mesmo identifier) e aguarda available."""
        self._assert_not_prod(from_id, "rename")
        self._assert_not_prod(to_id, "rename")
        logging.info("Renomeando cluster %s → %s...", from_id, to_id)
        self.rds.modify_db_cluster(
            DBClusterIdentifier=from_id,
            NewDBClusterIdentifier=to_id,
            ApplyImmediately=True,
        )
        self._wait_cluster(to_id, "available")

        logging.info("Renomeando instância %s → %s...", from_id, to_id)
        self.rds.modify_db_instance(
            DBInstanceIdentifier=from_id,
            NewDBInstanceIdentifier=to_id,
            ApplyImmediately=True,
        )
        self._wait_instance(to_id, "available")

    # ------------------------------------------------------------------
    # Pré-voo (já existentes)
    # ------------------------------------------------------------------

    def log_caller_identity(self) -> dict:
        """Loga conta/ARN em uso (útil depois de trocar access key)."""
        identity = self.sts.get_caller_identity()
        logging.info(
            "AWS identity: Account=%s | Arn=%s | região=%s",
            identity.get("Account"),
            identity.get("Arn"),
            self.region,
        )
        return identity

    def _find_managed_policy_by_name(self, policy_name: str) -> dict | None:
        """Localiza policy gerenciada (customer) pelo nome. Retorna None se não existir."""
        paginator = self.iam.get_paginator("list_policies")
        for page in paginator.paginate(Scope="Local"):
            for policy in page.get("Policies") or []:
                if policy.get("PolicyName") == policy_name:
                    return policy
        return None

    def _caller_has_policy_attached(self, policy_arn: str) -> bool:
        """True se a identity atual (role ou user) tem a policy anexada."""
        caller_arn = self.sts.get_caller_identity()["Arn"]
        if ":assumed-role/" in caller_arn:
            role_name = caller_arn.split(":assumed-role/")[1].split("/")[0]
            paginator = self.iam.get_paginator("list_attached_role_policies")
            for page in paginator.paginate(RoleName=role_name):
                for attached in page.get("AttachedPolicies") or []:
                    if attached.get("PolicyArn") == policy_arn:
                        return True
            return False
        if ":user/" in caller_arn:
            user_name = caller_arn.rsplit("/", 1)[-1]
            paginator = self.iam.get_paginator("list_attached_user_policies")
            for page in paginator.paginate(UserName=user_name):
                for attached in page.get("AttachedPolicies") or []:
                    if attached.get("PolicyArn") == policy_arn:
                        return True
            return False
        logging.warning(
            "Identity atual não é role nem user IAM reconhecível: %s", caller_arn
        )
        return False

    def check_avoid_delete_policy(
        self, policy_name: str = AVOID_DELETE_POLICY_NAME
    ) -> dict:
        """
        Checa se a policy IAM existe e se está ativa (anexada à identity atual).

        Returns:
            Dict com exists, active, policy_arn e attached_to_caller.
        """
        logging.info("Checando policy IAM '%s'...", policy_name)
        result = {
            "policy_name": policy_name,
            "exists": False,
            "active": False,
            "attached_to_caller": False,
            "policy_arn": None,
        }
        try:
            policy = self._find_managed_policy_by_name(policy_name)
        except Exception as error:
            logging.error("Falha ao listar policies IAM: %s", error)
            raise

        if not policy:
            logging.warning(
                "Policy '%s': NÃO encontrada na conta (customer managed).",
                policy_name,
            )
            return result

        policy_arn = policy.get("Arn")
        result["exists"] = True
        result["policy_arn"] = policy_arn
        logging.info(
            "Policy '%s': encontrada. ARN=%s | AttachmentCount=%s | DefaultVersion=%s",
            policy_name,
            policy_arn,
            policy.get("AttachmentCount"),
            policy.get("DefaultVersionId"),
        )

        try:
            attached_to_caller = self._caller_has_policy_attached(policy_arn)
        except Exception as error:
            logging.error(
                "Falha ao verificar anexação da policy na identity atual: %s",
                error,
            )
            raise

        result["attached_to_caller"] = attached_to_caller
        result["active"] = attached_to_caller

        if attached_to_caller:
            logging.info(
                "Policy '%s': ATIVA (anexada à identity atual).", policy_name
            )
        else:
            logging.warning(
                "Policy '%s': existe, mas NÃO está ativa na identity atual "
                "(não anexada à role/user que está executando).",
                policy_name,
            )

        return result

    def _writer_instance_info(
        self, cluster: dict
    ) -> tuple[str | None, str | None, bool | None]:
        """Retorna (writer_id, instance_class, publicly_accessible) do cluster."""
        members = cluster.get("DBClusterMembers") or []
        writer_id = None
        for member in members:
            if member.get("IsClusterWriter"):
                writer_id = member.get("DBInstanceIdentifier")
                break
        if not writer_id and members:
            writer_id = members[0].get("DBInstanceIdentifier")
        if not writer_id:
            return None, None, None
        try:
            response = self.rds.describe_db_instances(
                DBInstanceIdentifier=writer_id
            )
            inst = (response.get("DBInstances") or [None])[0]
            if not inst:
                return writer_id, None, None
            return (
                writer_id,
                inst.get("DBInstanceClass"),
                inst.get("PubliclyAccessible"),
            )
        except ClientError as error:
            logging.warning(
                "Não foi possível descrever writer %s: %s", writer_id, error
            )
            return writer_id, None, None

    @staticmethod
    def _publico_label(publicly_accessible: bool | None) -> str:
        if publicly_accessible is True:
            return "sim"
        if publicly_accessible is False:
            return "não"
        return "-"

    @staticmethod
    def _parse_optional_env_bool(key: str) -> bool | None:
        """Lê bool opcional do env: unset/vazio → None; true/false conhecidos; senão erro."""
        raw = os.getenv(key)
        if raw is None or not raw.strip():
            return None
        value = raw.strip().lower()
        if value in ("true", "1", "yes", "on"):
            return True
        if value in ("false", "0", "no", "off"):
            return False
        raise RuntimeError(
            f"{key}={raw!r} inválido. Use true/false, 1/0, yes/no ou on/off "
            "(ou omita para herdar do writer PROD)."
        )

    def _resolve_temp_publicly_accessible(
        self, prod_cluster: dict
    ) -> tuple[bool, str]:
        """
        Resolve PubliclyAccessible para a instância TEMP.

        Returns:
            (valor, origem) onde origem é "env" ou "prod".
        """
        override = self._parse_optional_env_bool("DEV_PUBLICLY_ACCESSIBLE")
        if override is not None:
            return override, "env"

        _, _, publicly_accessible = self._writer_instance_info(prod_cluster)
        if publicly_accessible is None:
            logging.warning(
                "PubliclyAccessible do writer PROD indisponível; "
                "usando False no TEMP (seguro)."
            )
            return False, "prod"
        return publicly_accessible, "prod"

    @staticmethod
    def _sg_ids_from_resource(resource: dict) -> list[str]:
        groups = resource.get("VpcSecurityGroups") or []
        return [
            g.get("VpcSecurityGroupId")
            for g in groups
            if g.get("VpcSecurityGroupId")
        ]

    def list_rds(self) -> list[dict]:
        """
        Lista clusters RDS e loga identifier, status, engine, rede e endpoint.

        Inclui subnet group, security groups, instance class, publico (writer)
        e master_user (útil para conferir DB_USER no .env).

        Returns:
            Lista de dicts com os campos relevantes de cada cluster.
        """
        self.log_caller_identity()
        try:
            response = self.rds.describe_db_clusters()
        except Exception as error:
            logging.error("Falha ao listar clusters RDS: %s", error)
            raise

        clusters = response.get("DBClusters") or []
        if not clusters:
            logging.warning(
                "Nenhum cluster Aurora/RDS encontrado na região %s "
                "(describe_db_clusters não lista instâncias standalone).",
                self.region,
            )
            try:
                instances = (
                    self.rds.describe_db_instances().get("DBInstances") or []
                )
            except Exception as error:
                logging.error("Falha ao listar instâncias RDS: %s", error)
                raise
            if instances:
                logging.info(
                    "Instâncias RDS (não-cluster) na região %s: %s",
                    self.region,
                    len(instances),
                )
                for inst in instances:
                    sg_ids = self._sg_ids_from_resource(inst)
                    subnet = (inst.get("DBSubnetGroup") or {}).get(
                        "DBSubnetGroupName"
                    ) or "-"
                    publico = self._publico_label(inst.get("PubliclyAccessible"))
                    logging.info(
                        "Instance %s\n"
                        "  status:          %s\n"
                        "  engine:          %s\n"
                        "  cluster:         %s\n"
                        "  subnet:          %s\n"
                        "  sg:              %s\n"
                        "  instance_class:  %s\n"
                        "  publico:         %s",
                        inst.get("DBInstanceIdentifier"),
                        inst.get("DBInstanceStatus"),
                        inst.get("Engine"),
                        inst.get("DBClusterIdentifier") or "-",
                        subnet,
                        ",".join(sg_ids) or "-",
                        inst.get("DBInstanceClass") or "-",
                        publico,
                    )
            return []

        result: list[dict] = []
        logging.info(
            "Clusters RDS encontrados na região %s: %s", self.region, len(clusters)
        )
        for cluster in clusters:
            sg_ids = self._sg_ids_from_resource(cluster)
            writer_id, instance_class, publicly_accessible = (
                self._writer_instance_info(cluster)
            )
            master_user = cluster.get("MasterUsername") or "-"
            item = {
                "identifier": cluster.get("DBClusterIdentifier"),
                "status": cluster.get("Status"),
                "engine": cluster.get("Engine"),
                "engine_version": cluster.get("EngineVersion"),
                "endpoint": cluster.get("Endpoint"),
                "region": self.region,
                "subnet_group": cluster.get("DBSubnetGroup") or "-",
                "vpc_security_group_ids": sg_ids,
                "writer_instance": writer_id,
                "instance_class": instance_class,
                "publicly_accessible": publicly_accessible,
                "master_user": master_user,
            }
            result.append(item)
            logging.info(
                "Cluster %s\n"
                "  status:          %s\n"
                "  engine:          %s (%s)\n"
                "  region:          %s\n"
                "  subnet:          %s\n"
                "  sg:              %s\n"
                "  writer:          %s\n"
                "  instance_class:  %s\n"
                "  publico:         %s\n"
                "  master_user:     %s\n"
                "  endpoint:        %s",
                item["identifier"],
                item["status"],
                item["engine"],
                item["engine_version"],
                item["region"],
                item["subnet_group"],
                ",".join(sg_ids) or "-",
                writer_id or "-",
                instance_class or "-",
                self._publico_label(publicly_accessible),
                master_user,
                item["endpoint"],
            )

        return result

    # ------------------------------------------------------------------
    # Pipeline steps 1–7
    # ------------------------------------------------------------------

    def clone_prod_to_temp(self) -> None:
        """1. Clone PROD → TEMP (PITR copy-on-write) + cria writer + credenciais DEV."""
        self._resolve_db_credentials()
        self._require_env(
            "PROD_IDENTIFIER",
            "DEV_IDENTIFIER",
            "TEMP_IDENTIFIER",
            "DB_SUBNET_GROUP",
            "VPC_SECURITY_GROUP_IDS",
            "DB_PASSWORD",
        )
        if self._cluster_exists(self.temp_id):
            raise RuntimeError(
                f"TEMP já existe ({self.temp_id}). Remova antes de clonar."
            )

        prod_response = self.rds.describe_db_clusters(
            DBClusterIdentifier=self.prod_id
        )
        prod_cluster = (prod_response.get("DBClusters") or [None])[0]
        if not prod_cluster:
            raise RuntimeError(f"Cluster PROD não encontrado: {self.prod_id}")

        publicly_accessible, publico_origem = (
            self._resolve_temp_publicly_accessible(prod_cluster)
        )

        logging.info(
            "Clonando PROD %s → TEMP %s (copy-on-write, latest PITR)...",
            self.prod_id,
            self.temp_id,
        )
        self.rds.restore_db_cluster_to_point_in_time(
            DBClusterIdentifier=self.temp_id,
            SourceDBClusterIdentifier=self.prod_id,
            RestoreType="copy-on-write",
            UseLatestRestorableTime=True,
            DBSubnetGroupName=self.db_subnet_group,
            VpcSecurityGroupIds=self.vpc_security_group_ids,
        )
        self._wait_cluster(self.temp_id, "available")

        logging.info(
            "Criando instância %s (class=%s, engine=%s, publico: %s, origem: %s)...",
            self.temp_id,
            self.db_instance_class,
            self.engine,
            self._publico_label(publicly_accessible),
            publico_origem,
        )
        self.rds.create_db_instance(
            DBInstanceIdentifier=self.temp_id,
            DBClusterIdentifier=self.temp_id,
            DBInstanceClass=self.db_instance_class,
            Engine=self.engine,
            PubliclyAccessible=publicly_accessible,
        )
        self._wait_instance(self.temp_id, "available")

        self._reset_temp_master_password()
        master_username = self._cluster_master_username(self.temp_id)
        self._ensure_temp_db_user(master_username)
        logging.info("Clone PROD→TEMP concluído: %s", self.temp_id)

    def dump_dev_schemas(self) -> Path:
        """2. Dump dos schemas DEV com mydumper."""
        self._resolve_db_credentials()
        self._require_env(
            "PROD_IDENTIFIER",
            "DEV_IDENTIFIER",
            "TEMP_IDENTIFIER",
            "DB_PASSWORD",
        )
        schemas = self._resolve_dev_only_schemas()
        endpoint = self._cluster_endpoint(self.dev_id)
        dump_dir = self._dump_dir_today()
        logging.info(
            "Dump DEV %s (%s) schemas=%s → %s",
            self.dev_id,
            endpoint,
            schemas,
            dump_dir,
        )
        for schema in schemas:
            schema_dir = dump_dir / schema
            schema_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                "mydumper",
                "-h",
                endpoint,
                "-u",
                self.db_user,
                "-p",
                self.db_password,
                "-B",
                schema,
                "-o",
                str(schema_dir),
                "--routines",
                "--triggers",
                "--events",
                "--compress",
                "--threads",
                "4",
                "--no-locks",
            ]
            logging.info("mydumper schema=%s → %s", schema, schema_dir)
            subprocess.run(cmd, check=True)
        logging.info("Dump DEV concluído em %s", dump_dir)
        return dump_dir

    def restore_dump_to_temp(self) -> None:
        """3. Restore dump DEV → TEMP com myloader (--overwrite-tables)."""
        self._resolve_db_credentials()
        self._require_env(
            "PROD_IDENTIFIER",
            "DEV_IDENTIFIER",
            "TEMP_IDENTIFIER",
            "DB_PASSWORD",
        )
        schemas = self._resolve_dev_only_schemas()
        endpoint = self._cluster_endpoint(self.temp_id)
        dump_dir = self._dump_dir_today()
        logging.info(
            "Restore dump %s → TEMP %s (%s) schemas=%s",
            dump_dir,
            self.temp_id,
            endpoint,
            schemas,
        )
        for schema in schemas:
            schema_dir = dump_dir / schema
            if not schema_dir.is_dir():
                raise RuntimeError(
                    f"Pasta de dump ausente para schema {schema}: {schema_dir}"
                )
            cmd = [
                "myloader",
                "-h",
                endpoint,
                "-u",
                self.db_user,
                "-p",
                self.db_password,
                "-d",
                str(schema_dir),
                "--overwrite-tables",
                "--threads",
                "4",
            ]
            logging.info("myloader schema=%s → TEMP", schema)
            subprocess.run(cmd, check=True)
        logging.info("Restore dump→TEMP concluído.")

    def validate_dev_vs_temp(self) -> dict:
        """
        4. Compara COUNT(*) DEV vs TEMP nos schemas dumpados.
        Em falha: apaga TEMP e aborta (DEV intacto).
        """
        self._resolve_db_credentials()
        self._require_env(
            "PROD_IDENTIFIER",
            "DEV_IDENTIFIER",
            "TEMP_IDENTIFIER",
            "DB_PASSWORD",
        )
        schemas = self._resolve_restored_schemas()
        dev_host = self._cluster_endpoint(self.dev_id)
        temp_host = self._cluster_endpoint(self.temp_id)
        logging.info(
            "Validando row counts DEV (%s) vs TEMP (%s), schemas=%s, "
            "tolerância=%s",
            dev_host,
            temp_host,
            schemas,
            self.row_diff_tolerance,
        )
        try:
            dev_counts = self._table_row_counts(dev_host, schemas)
            temp_counts = self._table_row_counts(temp_host, schemas)
        except Exception:
            logging.error(
                "Falha ao ler counts; apagando TEMP %s e abortando.",
                self.temp_id,
            )
            self._delete_cluster_and_instance(self.temp_id)
            raise

        all_keys = sorted(set(dev_counts) | set(temp_counts))
        comparisons: list[dict] = []
        divergences: list[dict] = []
        for key in all_keys:
            left = dev_counts.get(key, 0)
            right = temp_counts.get(key, 0)
            diff = abs(left - right)
            within = diff <= self.row_diff_tolerance
            row = {
                "table": key,
                "dev": left,
                "temp": right,
                "diff": diff,
                "ok": within,
            }
            comparisons.append(row)
            logging.info(
                "  %s: DEV=%s  TEMP=%s  |diff|=%s  "
                "(tolerância=%s → %s)",
                key,
                left,
                right,
                diff,
                self.row_diff_tolerance,
                "OK" if within else "FORA",
            )
            if not within:
                divergences.append(row)
                logging.warning(
                    "Divergência %s: DEV=%s TEMP=%s |diff|=%s > tolerância=%s",
                    key,
                    left,
                    right,
                    diff,
                    self.row_diff_tolerance,
                )

        max_diff = max((c["diff"] for c in comparisons), default=0)
        total_dev = sum(c["dev"] for c in comparisons)
        total_temp = sum(c["temp"] for c in comparisons)
        logging.info(
            "Resumo: %s tabela(s) | DEV total=%s | TEMP total=%s | "
            "|DEV−TEMP| total=%s | max |diff| tabela=%s | "
            "tolerância por tabela=%s",
            len(comparisons),
            total_dev,
            total_temp,
            abs(total_dev - total_temp),
            max_diff,
            self.row_diff_tolerance,
        )

        if divergences:
            artifact = self._dump_dir_today() / "validate_divergences.txt"
            lines = [
                f"{d['table']}: DEV={d['dev']} TEMP={d['temp']} "
                f"|diff|={d['diff']} > tolerância={self.row_diff_tolerance}"
                for d in divergences
            ]
            artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")
            logging.error(
                "Validação falhou (%s/%s tabelas fora da tolerância %s). "
                "Artefato: %s. Apagando TEMP.",
                len(divergences),
                len(comparisons),
                self.row_diff_tolerance,
                artifact,
            )
            self._delete_cluster_and_instance(self.temp_id)
            raise RuntimeError(
                f"Validação DEV vs TEMP falhou: {len(divergences)} tabela(s) "
                f"acima da tolerância {self.row_diff_tolerance}."
            )

        result = {
            "ok": True,
            "tables_compared": len(all_keys),
            "tolerance": self.row_diff_tolerance,
            "total_dev": total_dev,
            "total_temp": total_temp,
            "max_table_diff": max_diff,
        }
        logging.info(
            "Validação OK: %s tabela(s) dentro da tolerância=%s "
            "(max |diff|=%s ≤ %s)",
            result["tables_compared"],
            result["tolerance"],
            max_diff,
            self.row_diff_tolerance,
        )
        return result

    def rename_dev_to_old(self) -> None:
        """5. Renomeia DEV → OLD (cluster + writer)."""
        self._require_env("DEV_IDENTIFIER", "OLD_IDENTIFIER", "PROD_IDENTIFIER")
        self._assert_not_prod(self.dev_id, "rename_dev_to_old")
        self._assert_not_prod(self.old_id, "rename_dev_to_old")
        if self._cluster_exists(self.old_id):
            raise RuntimeError(
                f"OLD já existe ({self.old_id}). Remova antes do rename."
            )
        self._rename_cluster_and_instance(self.dev_id, self.old_id)
        logging.info("Rename DEV→OLD concluído: %s", self.old_id)

    def rename_temp_to_dev(self) -> None:
        """6. Renomeia TEMP → DEV (cluster + writer)."""
        self._require_env("TEMP_IDENTIFIER", "DEV_IDENTIFIER", "PROD_IDENTIFIER")
        self._assert_not_prod(self.temp_id, "rename_temp_to_dev")
        self._assert_not_prod(self.dev_id, "rename_temp_to_dev")
        self._rename_cluster_and_instance(self.temp_id, self.dev_id)
        logging.info("Rename TEMP→DEV concluído: %s", self.dev_id)

    def delete_old_dev(self) -> None:
        """7. Apaga OLD (ou só loga se KEEP_OLD_DEV_HOURS > 0)."""
        self._require_env("OLD_IDENTIFIER", "PROD_IDENTIFIER")
        self._assert_not_prod(self.old_id, "delete_old_dev")
        if self.keep_old_dev_hours > 0:
            logging.info(
                "KEEP_OLD_DEV_HOURS=%s > 0: NÃO apagando OLD %s "
                "(rollback manual disponível).",
                self.keep_old_dev_hours,
                self.old_id,
            )
            return
        logging.info("Apagando OLD %s...", self.old_id)
        self._delete_cluster_and_instance(self.old_id)
        logging.info("Delete OLD concluído.")

    def run_pipeline(self) -> None:
        """Executa passos 1→7 na ordem."""
        logging.info("=== Pipeline Aurora Clone: início ===")
        self._resolve_db_credentials()
        self.clone_prod_to_temp()
        self.dump_dev_schemas()
        self.restore_dump_to_temp()
        self.validate_dev_vs_temp()
        self.rename_dev_to_old()
        self.rename_temp_to_dev()
        self.delete_old_dev()
        logging.info("=== Pipeline Aurora Clone: concluída ===")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aurora Clone — pipeline PROD→DEV (passos 1–7)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("account-aws", help="Mostra Account/Arn AWS em uso")
    sub.add_parser("check-policy", help="Checa policy AvoidDeleteAuroraCloneBoto3")
    sub.add_parser("list-rds", help="Lista clusters RDS e engines")
    sub.add_parser("clone-prod-to-temp", help="1. Clone PROD → TEMP")
    sub.add_parser("dump-dev", help="2. Dump schemas DEV (mydumper)")
    sub.add_parser("restore-temp", help="3. Restore dump → TEMP (myloader)")
    sub.add_parser("validate", help="4. Valida DEV vs TEMP (row counts)")
    sub.add_parser("rename-dev-to-old", help="5. Rename DEV → OLD")
    sub.add_parser("rename-temp-to-dev", help="6. Rename TEMP → DEV")
    sub.add_parser("delete-old-dev", help="7. Delete OLD")
    sub.add_parser("pipeline", help="Roda passos 1→7")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = AuroraClone()
    command = args.command or "pipeline"

    if command == "account-aws":
        app.log_caller_identity()
    elif command == "check-policy":
        result = app.check_avoid_delete_policy()
        logging.info(
            "Resultado policy '%s': exists=%s | active=%s | arn=%s",
            result["policy_name"],
            result["exists"],
            result["active"],
            result["policy_arn"],
        )
    elif command == "list-rds":
        clusters = app.list_rds()
        logging.info(
            "Listagem concluída. Região: %s. Total: %s cluster(s).",
            app.region,
            len(clusters),
        )
    elif command == "clone-prod-to-temp":
        app.clone_prod_to_temp()
    elif command == "dump-dev":
        app.dump_dev_schemas()
    elif command == "restore-temp":
        app.restore_dump_to_temp()
    elif command == "validate":
        app.validate_dev_vs_temp()
    elif command == "rename-dev-to-old":
        app.rename_dev_to_old()
    elif command == "rename-temp-to-dev":
        app.rename_temp_to_dev()
    elif command == "delete-old-dev":
        app.delete_old_dev()
    elif command == "pipeline":
        app.run_pipeline()
    else:
        parser.error(f"Comando desconhecido: {command}")


if __name__ == "__main__":
    main()
