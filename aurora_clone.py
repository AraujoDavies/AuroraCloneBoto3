"""
Aurora Clone — versão pública.

Pipeline: clone PROD→TEMP, sanitize TEMP, dump DEV, restore→TEMP,
validate, rename DEV→OLD / TEMP→DEV, delete OLD.
Pré-voo: inventário RDS.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import boto3
import psycopg2
import pymysql
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from psycopg2 import sql as pgsql
from psycopg2.extras import RealDictCursor

# override=True: após trocar a secret key, o arquivo vence variáveis antigas do shell.
load_dotenv(override=True)

TEMP_REQUIRED_UNAVAILABLE = (
    "banco de dados temp, é obrigatório mas não está disponível no momento."
)

logging.basicConfig(
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s - %(levelname)s: %(message)s",
)

POLL_INTERVAL_SECONDS = 30
SANITIZE_RETRY_WAIT_SECONDS = 60
TEMP_MASTER_LOGIN_MAX_ATTEMPTS = 5
TEMP_MASTER_LOGIN_RETRY_WAIT_SECONDS = 30
SANITIZE_LOG_OUTPUT_MAX_BYTES = 32 * 1024
PIPELINE_LOG_NAME = "pipeline.log"
_RUN_DIR_NAME_RE = re.compile(r"^\d{8}_\d{6}$")
# Válido em MySQL e PostgreSQL quando SANITIZE_SQL_PATH está vazio.
SANITIZE_LIST_SCHEMAS_SQL = (
    "SELECT schema_name FROM information_schema.schemata "
    "ORDER BY schema_name;"
)
SYSTEM_SCHEMAS_MYSQL = frozenset(
    {"mysql", "information_schema", "performance_schema", "sys"}
)
# Alias histórico (MySQL); listagem PG filtra via SQL, não esta lista.
SYSTEM_SCHEMAS = SYSTEM_SCHEMAS_MYSQL
DEFAULT_PG_DATABASE = "postgres"
PG_LIB_ROOT = Path("/usr/lib/postgresql")
PG_CLIENT_TOOLS = frozenset({"pg_dump", "pg_restore", "psql"})

# Estado compartilhado entre instâncias AuroraClone (ex.: etapas do game).
_active_run_dump_dir: Path | None = None
_active_file_handler: logging.FileHandler | None = None


def dump_base_dir_from_env() -> Path:
    return Path((os.getenv("DUMP_BASE_DIR") or "./dumps").strip())


def pin_active_run_dump_dir(path: Path) -> Path:
    """Fixa a pasta de run compartilhada (game: reabrir sem criar outro timestamp)."""
    global _active_run_dump_dir
    base = dump_base_dir_from_env()
    path = Path(path)
    if not _RUN_DIR_NAME_RE.match(path.name):
        raise ValueError(
            f"Nome de pasta de run inválido: {path.name!r} "
            "(esperado YYYYMMDD_HHMMSS)"
        )
    target = base / path.name
    if path != target:
        try:
            if path.resolve() != target.resolve():
                raise ValueError(
                    f"Pasta de run {path} não está sob DUMP_BASE_DIR ({base})"
                )
        except OSError as err:
            if path.parent != base:
                raise ValueError(
                    f"Pasta de run {path} não está sob DUMP_BASE_DIR ({base})"
                ) from err
    target.mkdir(parents=True, exist_ok=True)
    _active_run_dump_dir = target
    return target


def create_new_run_dump_dir(dump_base_dir: Path | None = None) -> Path:
    """Cria uma nova pasta YYYYMMDD_HHMMSS sob DUMP_BASE_DIR e a fixa como ativa."""
    global _active_run_dump_dir
    base = dump_base_dir if dump_base_dir is not None else dump_base_dir_from_env()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = base / run_id
    path.mkdir(parents=True, exist_ok=True)
    _active_run_dump_dir = path
    return path


def ensure_run_dump_dir(dump_base_dir: Path | None = None) -> Path:
    """Cria ou reutiliza a pasta de run YYYYMMDD_HHMMSS sob DUMP_BASE_DIR."""
    global _active_run_dump_dir
    if _active_run_dump_dir is not None:
        _active_run_dump_dir.mkdir(parents=True, exist_ok=True)
        return _active_run_dump_dir
    return create_new_run_dump_dir(dump_base_dir)


def start_run_file_log(dump_base_dir: Path | None = None) -> Path:
    """Anexa FileHandler em {run_dir}/pipeline.log (idempotente)."""
    global _active_file_handler
    run_dir = ensure_run_dump_dir(dump_base_dir)
    if _active_file_handler is not None:
        return run_dir
    log_path = run_dir / PIPELINE_LOG_NAME
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
    )
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    _active_file_handler = handler
    logging.info("Log de arquivo: %s", log_path)
    return run_dir


def stop_run_file_log() -> None:
    """Flush/close do FileHandler ativo, se houver."""
    global _active_file_handler
    handler = _active_file_handler
    if handler is None:
        return
    root = logging.getLogger()
    root.removeHandler(handler)
    try:
        handler.flush()
        handler.close()
    except Exception:
        pass
    _active_file_handler = None


def clear_active_run_dump_dir() -> None:
    """Libera a pasta de run compartilhada (próximo dump/pipeline cria outra)."""
    global _active_run_dump_dir
    _active_run_dump_dir = None


def latest_run_dump_dir(dump_base_dir: Path | None = None) -> Path | None:
    base = dump_base_dir if dump_base_dir is not None else dump_base_dir_from_env()
    if not base.is_dir():
        return None
    candidates = [
        p
        for p in base.iterdir()
        if p.is_dir() and _RUN_DIR_NAME_RE.match(p.name)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.name)


def mask_account_id(account: str | None) -> str:
    """Mascarar account id de 12 dígitos: 123***789."""
    account = (account or "").strip()
    if len(account) >= 6:
        return f"{account[:3]}***{account[-3:]}"
    return "***" if account else "***"


def mask_account_in_text(text: str | None, account: str | None) -> str:
    """Substitui o account id completo em ARNs/textos pelo valor mascarado."""
    raw = text or ""
    acct = (account or "").strip()
    if not raw or not acct:
        return raw
    return raw.replace(acct, mask_account_id(acct))


def _aws_session(region: str) -> boto3.Session:
    """Sessão boto3 isolada; credenciais explícitas do env evitam cache da sessão padrão."""
    kwargs: dict[str, str] = {"region_name": region}
    access_key = (os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        session_token = (os.getenv("AWS_SESSION_TOKEN") or "").strip()
        if session_token:
            kwargs["aws_session_token"] = session_token
    return boto3.Session(**kwargs)


class AuroraClone:
    """Orquestra o clone Aurora PROD → DEV (passos 1–8)."""

    def __init__(self, region_name: str | None = None):
        region = (
            (region_name or "").strip()
            or (os.getenv("AWS_DEFAULT_REGION") or "").strip()
            or (os.getenv("AWS_REGION") or "").strip()
            or None
        )
        if not region:
            raise RuntimeError(
                "Faltam variáveis de ambiente no .env: AWS_DEFAULT_REGION "
                "(ou AWS_REGION). Edite o .env e tente de novo."
            )
        session = _aws_session(region)
        try:
            self.rds = session.client("rds", region_name=region)
        except Exception as error:
            raise RuntimeError(f"boto3 RDS Failed: {error}") from error
        try:
            self.sts = session.client("sts")
        except Exception as error:
            raise RuntimeError(f"boto3 STS Failed: {error}") from error
        try:
            self.ssm = session.client("ssm", region_name=region)
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
        # Opcional: se vazio, clone herda do writer DEV (fallback db.serverless).
        self.db_instance_class = (os.getenv("DB_INSTANCE_CLASS") or "").strip()

        self.db_password = os.getenv("DB_PASSWORD") or ""
        self.db_password_ssm_param = (
            os.getenv("DB_PASSWORD_SSM_PARAM") or ""
        ).strip()
        self._db_credentials_resolved = False
        self._master_username_cache: dict[str, str] = {}
        self._engine_port_cache: dict[str, tuple[str, int]] = {}

        self.dev_schemas = [
            s.strip()
            for s in (os.getenv("DEV_SCHEMAS") or "").split(",")
            if s.strip()
        ]
        self.dump_base_dir = dump_base_dir_from_env()
        self.s3_bucket = (os.getenv("S3_BUCKET") or "").strip()
        self.s3_dump_prefix = (
            (os.getenv("S3_DUMP_PREFIX") or "").strip().strip("/")
        )
        self.dump_local_retention_days = int(
            os.getenv("DUMP_LOCAL_RETENTION_DAYS") or "15"
        )
        self.sanitize_sql_path = (
            os.getenv("SANITIZE_SQL_PATH") or ""
        ).strip()
        self.row_diff_tolerance = int(os.getenv("ROW_DIFF_TOLERANCE") or "50")
        self.wait_timeout_seconds = int(
            os.getenv("WAIT_TIMEOUT_SECONDS") or "3600"
        )
        self.keep_old_dev_hours = float(os.getenv("KEEP_OLD_DEV_HOURS") or "0")
        self._run_dump_dir: Path | None = _active_run_dump_dir
        self._s3 = None

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
            "DB_PASSWORD": self.db_password,
            "DEV_SCHEMAS": ",".join(self.dev_schemas),
            "SANITIZE_SQL_PATH": self.sanitize_sql_path,
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

    def _resolve_db_credentials(self) -> None:
        """Resolve DB_PASSWORD (env → SSM) no início do passo (idempotente)."""
        if self._db_credentials_resolved:
            return
        self._resolve_db_password()
        self._db_credentials_resolved = True

    def _mysql_user_for(
        self, identifier: str, *, role: str | None = None
    ) -> str:
        """MasterUsername do cluster (cache por identifier). Nome histórico; vale MySQL e PG."""
        return self._master_user_for(identifier, role=role)

    def _master_user_for(
        self, identifier: str, *, role: str | None = None
    ) -> str:
        """MasterUsername do cluster (cache por identifier)."""
        cached = self._master_username_cache.get(identifier)
        if cached:
            return cached
        username = self._cluster_master_username(identifier, role=role)
        self._master_username_cache[identifier] = username
        return username

    def _warn_if_master_dev_differs_from_prod(self) -> None:
        """Loga se MasterUsername DEV ≠ PROD (TEMP/cutover herda PROD)."""
        if not self.dev_id or not self.prod_id:
            return
        master_dev = self._mysql_user_for(self.dev_id, role="DEV")
        master_prod = self._mysql_user_for(self.prod_id, role="PROD")
        if master_dev == master_prod:
            logging.info(
                "MasterUsername DEV=PROD=%s (login operacional = master RDS).",
                master_dev,
            )
            return
        logging.warning(
            "MasterUsername DEV (%s) ≠ PROD (%s). Dump/login no DEV usam "
            "%s; TEMP e o DEV pós-cutover herdam o master da PROD (%s). "
            "A senha do master no TEMP é alinhada com DB_PASSWORD. "
            "Apps que ainda usam o nome antigo do DEV precisam atualizar o login.",
            master_dev,
            master_prod,
            master_dev,
            master_prod,
        )

    def verify_dev_login(self) -> dict:
        """
        Resolve senha e testa login no writer do DEV (MasterUsername RDS).
        MySQL (pymysql) ou PostgreSQL (psycopg2) conforme Engine do cluster.
        Retorna host/user/engine/port (sem senha).
        """
        self._require_env("DEV_IDENTIFIER")
        self._resolve_db_credentials()
        if not self.db_password:
            raise RuntimeError("DB_PASSWORD ficou vazia após resolve.")
        if self.prod_id:
            self._assert_homogeneous_engines(
                (self.dev_id, "DEV"),
                (self.prod_id, "PROD"),
            )
        user = self._mysql_user_for(self.dev_id, role="DEV")
        host = self._cluster_endpoint(self.dev_id, role="DEV")
        engine, port = self._cluster_engine_and_port(self.dev_id, role="DEV")
        try:
            with self._db_connect(
                host, port=port, user=user, engine=engine
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 AS ok")
                    row = cur.fetchone() or {}
                if row.get("ok") not in (1, True):
                    raise RuntimeError(
                        f"Smoke login DEV {self.dev_id} respondeu sem ok=1."
                    )
        except (pymysql.Error, psycopg2.Error) as error:
            raise RuntimeError(
                f"Falha ao conectar no DEV ({self.dev_id} @ {host}:{port}, "
                f"engine={engine}) como {user!r}: {error}. "
                "Confira MasterUsername (make list-rds), DB_PASSWORD e rede/SG."
            ) from error
        logging.info(
            "Smoke login DEV ok (cluster=%s user=%s host=%s:%s engine=%s).",
            self.dev_id,
            user,
            host,
            port,
            engine,
        )
        return {
            "cluster": self.dev_id,
            "host": host,
            "user": user,
            "engine": engine,
            "port": port,
        }

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

    def _cluster_not_found_message(self, identifier: str, role: str | None) -> str:
        if role == "DEV":
            return (
                f"Cluster do banco de dados de DEV ({identifier}) "
                "não foi encontrado!"
            )
        if role == "PROD":
            return (
                f"Cluster do banco de dados de PROD ({identifier}) "
                "não foi encontrado! Confira PROD_IDENTIFIER no .env "
                "e se o cluster existe na região."
            )
        if role == "TEMP":
            return (
                f"Cluster do banco de dados de TEMP ({identifier}) "
                "não foi encontrado!"
            )
        return f"Cluster do banco de dados ({identifier}) não foi encontrado!"

    def _describe_db_cluster(self, identifier: str, *, role: str | None = None):
        try:
            response = self.rds.describe_db_clusters(
                DBClusterIdentifier=identifier
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "DBClusterNotFoundFault":
                raise RuntimeError(
                    self._cluster_not_found_message(identifier, role)
                ) from error
            raise
        return response

    def _cluster_endpoint(self, identifier: str, *, role: str | None = None) -> str:
        response = self._describe_db_cluster(identifier, role=role)
        endpoint = response["DBClusters"][0].get("Endpoint")
        if not endpoint:
            raise RuntimeError(f"Cluster {identifier} sem Endpoint writer.")
        return endpoint

    def _cluster_master_username(
        self, identifier: str, *, role: str | None = None
    ) -> str:
        response = self._describe_db_cluster(identifier, role=role)
        username = (response["DBClusters"][0].get("MasterUsername") or "").strip()
        if not username:
            raise RuntimeError(f"Cluster {identifier} sem MasterUsername.")
        return username

    def _cluster_engine_and_port(
        self, identifier: str, *, role: str | None = None
    ) -> tuple[str, int]:
        """Engine (lower) e Port do cluster; default de porta por família."""
        cached = self._engine_port_cache.get(identifier)
        if cached:
            return cached
        response = self._describe_db_cluster(identifier, role=role)
        cluster = response["DBClusters"][0]
        engine = (cluster.get("Engine") or "").strip().lower()
        if not engine:
            raise RuntimeError(f"Cluster {identifier} sem Engine.")
        raw_port = cluster.get("Port")
        if raw_port is not None:
            port = int(raw_port)
        elif self._is_postgres_engine(engine):
            port = 5432
        else:
            port = 3306
        result = (engine, port)
        self._engine_port_cache[identifier] = result
        return result

    def _engine_for(
        self, identifier: str, *, role: str | None = None
    ) -> str:
        engine, _port = self._cluster_engine_and_port(identifier, role=role)
        return engine

    @staticmethod
    def _is_postgres_engine(engine: str) -> bool:
        return "postgres" in (engine or "").lower()

    @staticmethod
    def _is_mysql_engine(engine: str) -> bool:
        eng = (engine or "").lower()
        return "mysql" in eng or eng == "aurora"

    @staticmethod
    def _pg_major_from_engine_version(engine_version: str | None) -> int | None:
        if not engine_version:
            return None
        segment = str(engine_version).split(".", 1)[0].strip()
        if not segment or not segment.isdigit():
            return None
        return int(segment)

    @classmethod
    def _engine_family(cls, engine: str) -> str:
        if cls._is_postgres_engine(engine):
            return "postgres"
        if cls._is_mysql_engine(engine):
            return "mysql"
        raise RuntimeError(
            f"Engine não suportada: {engine!r} "
            "(esperado mysql/aurora-mysql ou postgres/aurora-postgresql)."
        )

    def _assert_homogeneous_engines(
        self, *items: tuple[str, str]
    ) -> str:
        """
        Garante que os clusters listados (identifier, role) são da mesma família.
        Retorna a família ('mysql' | 'postgres').
        """
        families: dict[str, tuple[str, str]] = {}
        for identifier, role in items:
            if not identifier:
                continue
            engine = self._engine_for(identifier, role=role)
            family = self._engine_family(engine)
            families[identifier] = (family, engine)
        unique = {fam for fam, _eng in families.values()}
        if len(unique) > 1:
            detail = ", ".join(
                f"{ident}={eng} ({fam})"
                for ident, (fam, eng) in families.items()
            )
            raise RuntimeError(
                "PROD/DEV/TEMP devem usar a mesma família de engine "
                f"(mysql ou postgres). Encontrado: {detail}."
            )
        if not unique:
            raise RuntimeError(
                "Nenhum cluster informado para checagem de engine."
            )
        return next(iter(unique))

    @staticmethod
    def _default_database(engine: str) -> str:
        if AuroraClone._is_postgres_engine(engine):
            return (
                (os.getenv("DB_NAME") or "").strip() or DEFAULT_PG_DATABASE
            )
        return ""

    @staticmethod
    def _truncate_for_log(
        text: str, limit: int = SANITIZE_LOG_OUTPUT_MAX_BYTES
    ) -> str:
        raw = text or ""
        if len(raw) <= limit:
            return raw
        omitted = len(raw) - limit
        return raw[:limit] + f"\n... [truncated {omitted} bytes]"

    def _log_sanitize_cli_output(
        self, label: str, output: str, *, level: int = logging.INFO
    ) -> None:
        text = self._truncate_for_log((output or "").strip())
        if text:
            logging.log(level, "%s:\n%s", label, text)

    def _run_sanitize_cli(
        self,
        *,
        host: str,
        port: int,
        engine: str,
        sql_path: Path | None = None,
        sql_statement: str | None = None,
    ) -> str:
        """
        Executa SQL na TEMP via mysql ou psql (arquivo ou statement).
        Retorna stdout+stderr combinados. Raise se exit ≠ 0.
        """
        if (sql_path is None) == (sql_statement is None):
            raise ValueError(
                "Informe exatamente um de: sql_path ou sql_statement."
            )

        env = os.environ.copy()

        if self._is_postgres_engine(engine):
            server_major = self._pg_server_major(
                host, port, self._mysql_user_for(self.temp_id, role="TEMP"), engine
            )
            psql_path, client_major = self._resolve_pg_client("psql", server_major)
            env["PGPASSWORD"] = self.db_password
            temp_user = self._mysql_user_for(self.temp_id, role="TEMP")
            cmd = [
                psql_path,
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                temp_user,
                "-d",
                self._default_database(engine),
                "-v",
                "ON_ERROR_STOP=1",
                "-e",
            ]
            if sql_path is not None:
                cmd.extend(["-f", str(sql_path)])
                logging.info(
                    "Sanitize TEMP via psql major=%s (%s) (%s:%s) file=%s",
                    client_major,
                    psql_path,
                    host,
                    port,
                    sql_path,
                )
            else:
                cmd.extend(["-c", sql_statement or ""])
                logging.info(
                    "Sanitize TEMP via psql major=%s (%s) (%s:%s) statement "
                    "(list schemas)",
                    client_major,
                    psql_path,
                    host,
                    port,
                )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        elif self._is_mysql_engine(engine):
            if not shutil.which("mysql"):
                raise RuntimeError("Cliente mysql não encontrado no PATH.")
            temp_user = self._mysql_user_for(self.temp_id, role="TEMP")
            cmd = [
                "mysql",
                "-h",
                host,
                "-P",
                str(port),
                "-u",
                temp_user,
                f"-p{self.db_password}",
                "--batch",
                "--raw",
                "-vv",
            ]
            if sql_path is not None:
                logging.info(
                    "Sanitize TEMP via mysql (%s:%s) file=%s",
                    host,
                    port,
                    sql_path,
                )
                with open(sql_path, "rb") as sql_fh:
                    result = subprocess.run(
                        cmd,
                        stdin=sql_fh,
                        capture_output=True,
                        text=True,
                        env=env,
                        check=False,
                    )
            else:
                cmd.extend(["-e", sql_statement or ""])
                logging.info(
                    "Sanitize TEMP via mysql (%s:%s) statement (list schemas)",
                    host,
                    port,
                )
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
        else:
            raise RuntimeError(
                f"Engine não suportada para sanitize: {engine!r} "
                "(esperado mysql/aurora-mysql ou postgres/aurora-postgresql)."
            )

        combined = "\n".join(
            part
            for part in ((result.stdout or ""), (result.stderr or ""))
            if part
        )
        if result.returncode != 0:
            self._log_sanitize_cli_output(
                "Sanitize CLI output (falha)",
                combined,
                level=logging.ERROR,
            )
            raise RuntimeError(
                f"Sanitize SQL falhou (exit={result.returncode}): "
                f"{self._truncate_for_log(combined, 2048) or '(sem output)'}"
            )
        self._log_sanitize_cli_output("Sanitize CLI output", combined)
        return combined

    def _s3_client(self):
        if self._s3 is None:
            session = _aws_session(self.region)
            self._s3 = session.client("s3", region_name=self.region)
        return self._s3

    def _ensure_run_dump_dir(self) -> Path:
        path = ensure_run_dump_dir(self.dump_base_dir)
        self._run_dump_dir = path
        return path

    def _resolve_run_dump_dir(self) -> Path:
        """Pasta de run existente (dump/restore/validate/archive)."""
        global _active_run_dump_dir
        if self._run_dump_dir is not None and self._run_dump_dir.is_dir():
            return self._run_dump_dir
        if _active_run_dump_dir is not None and _active_run_dump_dir.is_dir():
            self._run_dump_dir = _active_run_dump_dir
            return self._run_dump_dir
        latest = latest_run_dump_dir(self.dump_base_dir)
        if latest is None:
            raise RuntimeError(
                f"Nenhuma pasta de run (YYYYMMDD_HHMMSS) em "
                f"{self.dump_base_dir}. Rode dump-dev antes."
            )
        self._run_dump_dir = latest
        _active_run_dump_dir = latest
        return latest

    def _prune_old_dump_runs(self) -> None:
        """Remove pastas sob DUMP_BASE_DIR com idade > retenção local."""
        days = self.dump_local_retention_days
        if days <= 0:
            return
        base = self.dump_base_dir
        if not base.is_dir():
            return
        cutoff = datetime.now().timestamp() - (days * 86400)
        active = None
        if self._run_dump_dir is not None:
            try:
                active = self._run_dump_dir.resolve()
            except OSError:
                active = self._run_dump_dir
        elif _active_run_dump_dir is not None:
            try:
                active = _active_run_dump_dir.resolve()
            except OSError:
                active = _active_run_dump_dir

        for path in list(base.iterdir()):
            if not path.is_dir():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if active is not None and resolved == active:
                continue
            age_ts: float | None = None
            if _RUN_DIR_NAME_RE.match(path.name):
                try:
                    age_ts = datetime.strptime(
                        path.name, "%Y%m%d_%H%M%S"
                    ).timestamp()
                except ValueError:
                    age_ts = None
            if age_ts is None:
                try:
                    age_ts = path.stat().st_mtime
                except OSError as err:
                    logging.warning(
                        "Falha ao inspecionar dump antigo %s: %s", path, err
                    )
                    continue
            if age_ts >= cutoff:
                continue
            try:
                shutil.rmtree(path)
                logging.info("Prune dump run antigo: %s", path)
            except OSError as err:
                logging.warning(
                    "Falha ao limpar dump antigo %s: %s", path, err
                )

    def start_run_file_log(self) -> Path:
        path = start_run_file_log(self.dump_base_dir)
        self._run_dump_dir = path
        return path

    def stop_run_file_log(self) -> None:
        stop_run_file_log()

    def release_run_dump_dir(self) -> None:
        clear_active_run_dump_dir()
        self._run_dump_dir = None

    def archive_run_to_s3(self) -> dict:
        """
        Sobe pipeline.log e depois o restante da pasta de run para S3.
        Soft-fail: erros viram warning e não propagam.
        """
        result: dict = {
            "skipped": True,
            "ok": True,
            "uri": None,
            "error": None,
            "run_dir": None,
        }
        if not self.s3_bucket:
            logging.info("S3_BUCKET vazio — archive S3 pulado.")
            return result

        result["skipped"] = False
        try:
            run_dir = self._resolve_run_dump_dir()
        except RuntimeError as err:
            logging.warning("Archive S3: %s", err)
            result["ok"] = False
            result["error"] = str(err)
            return result

        result["run_dir"] = str(run_dir)
        try:
            s3 = self._s3_client()
            parts = [p for p in (self.s3_dump_prefix, run_dir.name) if p]
            base_key = "/".join(parts)
            log_path = run_dir / PIPELINE_LOG_NAME
            if log_path.is_file():
                key = f"{base_key}/{PIPELINE_LOG_NAME}"
                s3.upload_file(str(log_path), self.s3_bucket, key)
                logging.info(
                    "S3 upload log: s3://%s/%s", self.s3_bucket, key
                )

            for path in sorted(run_dir.rglob("*")):
                if not path.is_file():
                    continue
                if (
                    path.name == PIPELINE_LOG_NAME
                    and path.parent.resolve() == run_dir.resolve()
                ):
                    continue
                rel = path.relative_to(run_dir).as_posix()
                key = f"{base_key}/{rel}"
                s3.upload_file(str(path), self.s3_bucket, key)
                logging.info("S3 upload: s3://%s/%s", self.s3_bucket, key)

            uri = f"s3://{self.s3_bucket}/{base_key}/"
            logging.info("Archive S3 concluído: %s", uri)
            result["uri"] = uri
            result["ok"] = True
            return result
        except Exception as err:
            logging.warning("Archive S3 falhou (continuando): %s", err)
            result["ok"] = False
            result["error"] = str(err)
            return result

    def _mysql_connect(
        self,
        host: str,
        *,
        user: str,
        password: str | None = None,
        port: int = 3306,
    ):
        return pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password if password is not None else self.db_password,
            connect_timeout=30,
            read_timeout=120,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _postgres_connect(
        self,
        host: str,
        *,
        user: str,
        password: str | None = None,
        port: int = 5432,
        database: str | None = None,
    ):
        return psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password if password is not None else self.db_password,
            dbname=database or DEFAULT_PG_DATABASE,
            connect_timeout=30,
            cursor_factory=RealDictCursor,
        )

    @staticmethod
    def _installed_pg_client_majors() -> list[int]:
        if not PG_LIB_ROOT.is_dir():
            return []
        majors: list[int] = []
        for entry in PG_LIB_ROOT.iterdir():
            if not entry.is_dir():
                continue
            try:
                major = int(entry.name)
            except ValueError:
                continue
            bin_dir = entry / "bin"
            if bin_dir.is_dir():
                majors.append(major)
        return sorted(majors)

    @staticmethod
    def _select_pg_client_major(installed: list[int], required: int) -> int:
        candidates = [major for major in installed if major >= required]
        if not candidates:
            installed_text = ", ".join(str(m) for m in installed) or "(nenhuma)"
            raise RuntimeError(
                f"Cliente PostgreSQL insuficiente para servidor major {required}. "
                f"Instaladas: {installed_text}."
            )
        return min(candidates)

    @staticmethod
    def _pg_tool_version_major(tool_path: str) -> int | None:
        try:
            result = subprocess.run(
                [tool_path, "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        match = re.search(r"(\d+)\.", (result.stdout or result.stderr or ""))
        if not match:
            return None
        return int(match.group(1))

    def _resolve_pg_client(
        self, tool: str, required_major: int
    ) -> tuple[str, int]:
        if tool not in PG_CLIENT_TOOLS:
            raise ValueError(f"Ferramenta PostgreSQL desconhecida: {tool!r}")
        installed = self._installed_pg_client_majors()
        if installed:
            client_major = self._select_pg_client_major(installed, required_major)
            path = PG_LIB_ROOT / str(client_major) / "bin" / tool
            if path.is_file():
                return (str(path), client_major)
        fallback = shutil.which(tool)
        if fallback:
            version_major = self._pg_tool_version_major(fallback)
            if version_major is not None and version_major >= required_major:
                return (fallback, version_major)
        installed_text = ", ".join(str(m) for m in installed) or "(nenhuma)"
        raise RuntimeError(
            f"Cliente {tool} não encontrado para major >= {required_major}. "
            f"Instaladas em {PG_LIB_ROOT}: {installed_text}."
        )

    def _pg_server_major(
        self, host: str, port: int, user: str, engine: str
    ) -> int:
        conn = self._postgres_connect(
            host,
            user=user,
            port=port,
            database=self._default_database(engine),
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW server_version_num")
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            raise RuntimeError(
                f"Não foi possível ler server_version_num em {host}:{port}."
            )
        version_num = row.get("server_version_num")
        if version_num is None and isinstance(row, (list, tuple)):
            version_num = row[0]
        if version_num is None:
            version_num = next(iter(row.values()))
        return int(version_num) // 10000

    @staticmethod
    def _read_dump_pg_client_major(dump_dir: Path) -> int | None:
        marker = dump_dir / ".pg_client_major"
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8").strip()
        if not text:
            return None
        return int(text)

    def _db_connect(
        self,
        host: str,
        *,
        port: int,
        user: str,
        engine: str,
        password: str | None = None,
    ):
        """Conexão programática MySQL (pymysql) ou PostgreSQL (psycopg2)."""
        if self._is_postgres_engine(engine):
            return self._postgres_connect(
                host,
                user=user,
                password=password,
                port=port,
                database=self._default_database(engine),
            )
        if self._is_mysql_engine(engine):
            return self._mysql_connect(
                host, user=user, password=password, port=port
            )
        raise RuntimeError(
            f"Engine não suportada para connect: {engine!r} "
            "(esperado mysql/aurora-mysql ou postgres/aurora-postgresql)."
        )

    def _db_connect_for(
        self,
        identifier: str,
        *,
        user: str,
        role: str | None = None,
        password: str | None = None,
    ):
        host = self._cluster_endpoint(identifier, role=role)
        engine, port = self._cluster_engine_and_port(identifier, role=role)
        return self._db_connect(
            host, port=port, user=user, engine=engine, password=password
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

    def _verify_temp_master_login(self) -> None:
        """Smoke login no TEMP com MasterUsername RDS + DB_PASSWORD.

        Retenta até TEMP_MASTER_LOGIN_MAX_ATTEMPTS com espera entre falhas
        (propagação lenta de MasterUserPassword após modify_db_cluster).
        """
        self._require_env("TEMP_IDENTIFIER", "DB_PASSWORD")
        user = self._mysql_user_for(self.temp_id, role="TEMP")
        endpoint = self._cluster_endpoint(self.temp_id, role="TEMP")
        engine, port = self._cluster_engine_and_port(
            self.temp_id, role="TEMP"
        )
        last_error: BaseException | None = None
        for attempt in range(1, TEMP_MASTER_LOGIN_MAX_ATTEMPTS + 1):
            logging.info(
                "Smoke login TEMP tentativa %s/%s "
                "(master=%s host=%s:%s engine=%s)...",
                attempt,
                TEMP_MASTER_LOGIN_MAX_ATTEMPTS,
                user,
                endpoint,
                port,
                engine,
            )
            try:
                with self._db_connect(
                    endpoint, port=port, user=user, engine=engine
                ):
                    pass
                logging.info(
                    "Smoke login TEMP ok (master=%s, tentativa %s).",
                    user,
                    attempt,
                )
                return
            except (pymysql.Error, psycopg2.Error) as error:
                last_error = error
                if attempt < TEMP_MASTER_LOGIN_MAX_ATTEMPTS:
                    logging.warning(
                        "Smoke login TEMP falhou (tentativa %s/%s): %s. "
                        "Aguardando %ss e retentando...",
                        attempt,
                        TEMP_MASTER_LOGIN_MAX_ATTEMPTS,
                        error,
                        TEMP_MASTER_LOGIN_RETRY_WAIT_SECONDS,
                    )
                    time.sleep(TEMP_MASTER_LOGIN_RETRY_WAIT_SECONDS)
                    continue
                raise RuntimeError(
                    f"Falha ao conectar no TEMP ({self.temp_id} @ "
                    f"{endpoint}:{port}, engine={engine}) "
                    f"como {user!r} após {TEMP_MASTER_LOGIN_MAX_ATTEMPTS} "
                    f"tentativas: {error}. "
                    "Confira MasterUsername (make list-rds), DB_PASSWORD e rede/SG."
                ) from error
        raise RuntimeError(
            f"Smoke login TEMP falhou: {last_error}"
        )

    def _list_user_schemas(
        self, identifier: str, *, user: str, role: str | None = None
    ) -> list[str]:
        """Lista schemas de usuário no cluster (exclui schemas de sistema)."""
        engine, _port = self._cluster_engine_and_port(identifier, role=role)
        with self._db_connect_for(
            identifier, user=user, role=role
        ) as conn:
            with conn.cursor() as cur:
                if self._is_postgres_engine(engine):
                    cur.execute(
                        """
                        SELECT schema_name AS schema_name
                        FROM information_schema.schemata
                        WHERE schema_name NOT IN (
                            'information_schema', 'pg_catalog'
                        )
                          AND schema_name !~ '^pg_'
                        ORDER BY schema_name
                        """
                    )
                    names = [row["schema_name"] for row in cur.fetchall()]
                    return sorted(names)
                cur.execute(
                    """
                    SELECT SCHEMA_NAME AS schema_name
                    FROM information_schema.SCHEMATA
                    ORDER BY SCHEMA_NAME
                    """
                )
                names = [row["schema_name"] for row in cur.fetchall()]
        return sorted(s for s in names if s not in SYSTEM_SCHEMAS_MYSQL)

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

        if not self.temp_id or not self._cluster_exists(self.temp_id):
            raise RuntimeError(TEMP_REQUIRED_UNAVAILABLE)

        try:
            # Touch endpoints early for clearer DBClusterNotFoundFault.
            self._cluster_endpoint(self.dev_id)
            self._cluster_endpoint(self.temp_id)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code == "DBClusterNotFoundFault":
                if not self._cluster_exists(self.temp_id):
                    raise RuntimeError(TEMP_REQUIRED_UNAVAILABLE) from error
                raise RuntimeError(
                    "Cluster DEV não encontrado. Confira DEV_IDENTIFIER ou "
                    "defina DEV_SCHEMAS."
                ) from error
            raise

        try:
            self._assert_homogeneous_engines(
                (self.dev_id, "DEV"),
                (self.temp_id, "TEMP"),
            )
            if self.prod_id:
                self._assert_homogeneous_engines(
                    (self.prod_id, "PROD"),
                    (self.dev_id, "DEV"),
                    (self.temp_id, "TEMP"),
                )
            dev_user = self._mysql_user_for(self.dev_id, role="DEV")
            temp_user = self._mysql_user_for(self.temp_id, role="TEMP")
            dev_schemas = self._list_user_schemas(
                self.dev_id, user=dev_user, role="DEV"
            )
            temp_schemas = self._list_user_schemas(
                self.temp_id, user=temp_user, role="TEMP"
            )
        except Exception as error:
            raise RuntimeError(
                "Falha ao listar schemas em DEV/TEMP. Confirme que TEMP "
                "está available e acessível com MasterUsername (list-rds) "
                f"e DB_PASSWORD, ou defina DEV_SCHEMAS. Detalhe: {error}"
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

        dump_dir = self._resolve_run_dump_dir()
        schemas = sorted(
            p.name
            for p in dump_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
        engine = self._engine_for(self.dev_id, role="DEV")
        if self._is_postgres_engine(engine):
            missing_dump = [
                s
                for s in schemas
                if not (dump_dir / s / f"{s}.dump").is_file()
            ]
            if missing_dump:
                raise RuntimeError(
                    "Dump PostgreSQL incompleto (falta *.dump) para: "
                    + ", ".join(missing_dump)
                    + f" em {dump_dir}."
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

    def _table_row_counts(
        self,
        identifier: str,
        schemas: list[str],
        *,
        user: str,
        role: str | None = None,
    ) -> dict[str, int]:
        """Retorna mapa schema.table → COUNT(*)."""
        engine, _port = self._cluster_engine_and_port(identifier, role=role)
        counts: dict[str, int] = {}
        with self._db_connect_for(
            identifier, user=user, role=role
        ) as conn:
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
                        if self._is_postgres_engine(engine):
                            cur.execute(
                                pgsql.SQL(
                                    "SELECT COUNT(*) AS cnt FROM {}.{}"
                                ).format(
                                    pgsql.Identifier(schema),
                                    pgsql.Identifier(table),
                                )
                            )
                        else:
                            cur.execute(
                                f"SELECT COUNT(*) AS cnt "
                                f"FROM `{schema}`.`{table}`"
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
        """Loga conta/ARN em uso (account mascarado nos logs)."""
        identity = self.sts.get_caller_identity()
        account = identity.get("Account") or ""
        arn = identity.get("Arn") or ""
        logging.info(
            "AWS identity: Account=%s | Arn=%s | região=%s",
            mask_account_id(account),
            mask_account_in_text(arn, account),
            self.region,
        )
        return identity

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
            "(ou omita para herdar do writer DEV)."
        )

    def _resolve_temp_publicly_accessible(
        self, dev_cluster: dict
    ) -> tuple[bool, str]:
        """
        Resolve PubliclyAccessible para a instância TEMP.

        Returns:
            (valor, origem) onde origem é "env" ou "dev".
        """
        override = self._parse_optional_env_bool("DEV_PUBLICLY_ACCESSIBLE")
        if override is not None:
            return override, "env"

        _, _, publicly_accessible = self._writer_instance_info(dev_cluster)
        if publicly_accessible is None:
            logging.warning(
                "PubliclyAccessible do writer DEV indisponível; "
                "usando False no TEMP (seguro)."
            )
            return False, "dev"
        return publicly_accessible, "dev"

    def _resolve_temp_placement(
        self, prod_cluster: dict, dev_cluster: dict
    ) -> dict:
        """
        Resolve subnet/SG/class/engine/public do TEMP.

        Env override quando setado; rede/porte do DEV; Engine sempre do PROD.
        """
        if self.db_subnet_group:
            subnet_group = self.db_subnet_group
            subnet_origem = "env"
        else:
            subnet_group = (dev_cluster.get("DBSubnetGroup") or "").strip()
            subnet_origem = "dev"
            if not subnet_group:
                raise RuntimeError(
                    "DB_SUBNET_GROUP não setado e cluster DEV sem DBSubnetGroup. "
                    "Defina DB_SUBNET_GROUP ou confira o DEV."
                )

        if self.vpc_security_group_ids:
            sg_ids = list(self.vpc_security_group_ids)
            sg_origem = "env"
        else:
            sg_ids = self._sg_ids_from_resource(dev_cluster)
            sg_origem = "dev"
            if not sg_ids:
                raise RuntimeError(
                    "VPC_SECURITY_GROUP_IDS não setado e cluster DEV sem "
                    "VpcSecurityGroups. Defina VPC_SECURITY_GROUP_IDS ou "
                    "confira o DEV."
                )

        if self.db_instance_class:
            instance_class = self.db_instance_class
            class_origem = "env"
        else:
            _, writer_class, _ = self._writer_instance_info(dev_cluster)
            if writer_class:
                instance_class = writer_class
                class_origem = "dev"
            else:
                instance_class = "db.serverless"
                class_origem = "default"
                logging.warning(
                    "DBInstanceClass do writer DEV indisponível; "
                    "usando db.serverless no TEMP."
                )

        engine = (prod_cluster.get("Engine") or "").strip()
        if not engine:
            raise RuntimeError(
                f"Cluster PROD ({self.prod_id}) sem Engine; "
                "não é possível criar a writer TEMP."
            )

        publicly_accessible, publico_origem = (
            self._resolve_temp_publicly_accessible(dev_cluster)
        )

        placement = {
            "subnet_group": subnet_group,
            "subnet_origem": subnet_origem,
            "vpc_security_group_ids": sg_ids,
            "sg_origem": sg_origem,
            "instance_class": instance_class,
            "class_origem": class_origem,
            "engine": engine,
            "engine_origem": "prod",
            "publicly_accessible": publicly_accessible,
            "publico_origem": publico_origem,
        }
        logging.info(
            "TEMP placement: subnet=%s (%s) sg=%s (%s) class=%s (%s) "
            "engine=%s (%s) publico=%s (%s)",
            placement["subnet_group"],
            placement["subnet_origem"],
            ",".join(placement["vpc_security_group_ids"]),
            placement["sg_origem"],
            placement["instance_class"],
            placement["class_origem"],
            placement["engine"],
            placement["engine_origem"],
            self._publico_label(placement["publicly_accessible"]),
            placement["publico_origem"],
        )
        return placement

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
        e master_user (login MySQL = MasterUsername do cluster; senha = DB_PASSWORD).

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
            engine = cluster.get("Engine")
            engine_version = cluster.get("EngineVersion")
            pg_major: int | None = None
            if self._is_postgres_engine(engine or ""):
                pg_major = self._pg_major_from_engine_version(engine_version)
            item = {
                "identifier": cluster.get("DBClusterIdentifier"),
                "status": cluster.get("Status"),
                "engine": engine,
                "engine_version": engine_version,
                "endpoint": cluster.get("Endpoint"),
                "port": cluster.get("Port"),
                "region": self.region,
                "subnet_group": cluster.get("DBSubnetGroup") or "-",
                "vpc_security_group_ids": sg_ids,
                "writer_instance": writer_id,
                "instance_class": instance_class,
                "publicly_accessible": publicly_accessible,
                "master_user": master_user,
            }
            if pg_major is not None:
                item["pg_major"] = pg_major
            result.append(item)
            if pg_major is not None:
                logging.info(
                    "Cluster %s\n"
                    "  identifier:      %s\n"
                    "  status:          %s\n"
                    "  engine:          %s (%s)\n"
                    "  major:           %s\n"
                    "  region:          %s\n"
                    "  subnet:          %s\n"
                    "  sg:              %s\n"
                    "  writer:          %s\n"
                    "  instance_class:  %s\n"
                    "  publico:         %s\n"
                    "  master_user:     %s\n"
                    "  endpoint:        %s",
                    item["identifier"],
                    item["identifier"],
                    item["status"],
                    item["engine"],
                    item["engine_version"],
                    pg_major,
                    item["region"],
                    item["subnet_group"],
                    ",".join(sg_ids) or "-",
                    writer_id or "-",
                    instance_class or "-",
                    self._publico_label(publicly_accessible),
                    master_user,
                    item["endpoint"],
                )
            else:
                logging.info(
                    "Cluster %s\n"
                    "  identifier:      %s\n"
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
    # Pipeline steps 1–8
    # ------------------------------------------------------------------

    def clone_prod_to_temp(self) -> None:
        """1. Clone PROD → TEMP (PITR copy-on-write) + writer + senha master TEMP."""
        self._resolve_db_credentials()
        self._require_env(
            "PROD_IDENTIFIER",
            "DEV_IDENTIFIER",
            "TEMP_IDENTIFIER",
            "DB_PASSWORD",
        )
        if self._cluster_exists(self.temp_id):
            raise RuntimeError(
                f"TEMP já existe ({self.temp_id}). Remova antes de clonar."
            )

        prod_response = self._describe_db_cluster(self.prod_id, role="PROD")
        prod_cluster = (prod_response.get("DBClusters") or [None])[0]
        if not prod_cluster:
            raise RuntimeError(
                self._cluster_not_found_message(self.prod_id, "PROD")
            )

        dev_response = self._describe_db_cluster(self.dev_id, role="DEV")
        dev_cluster = (dev_response.get("DBClusters") or [None])[0]
        if not dev_cluster:
            raise RuntimeError(
                self._cluster_not_found_message(self.dev_id, "DEV")
            )

        self._warn_if_master_dev_differs_from_prod()
        placement = self._resolve_temp_placement(prod_cluster, dev_cluster)

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
            DBSubnetGroupName=placement["subnet_group"],
            VpcSecurityGroupIds=placement["vpc_security_group_ids"],
        )
        self._wait_cluster(self.temp_id, "available")

        logging.info(
            "Criando instância %s (class=%s/%s, engine=%s/%s, "
            "publico: %s/%s)...",
            self.temp_id,
            placement["instance_class"],
            placement["class_origem"],
            placement["engine"],
            placement["engine_origem"],
            self._publico_label(placement["publicly_accessible"]),
            placement["publico_origem"],
        )
        self.rds.create_db_instance(
            DBInstanceIdentifier=self.temp_id,
            DBClusterIdentifier=self.temp_id,
            DBInstanceClass=placement["instance_class"],
            Engine=placement["engine"],
            PubliclyAccessible=placement["publicly_accessible"],
        )
        self._wait_instance(self.temp_id, "available")

        self._reset_temp_master_password()
        self._verify_temp_master_login()
        logging.info("Clone PROD→TEMP concluído: %s", self.temp_id)

    def sanitize_temp_data(
        self,
        *,
        max_attempts: int = 2,
        preserve_temp_on_failure: bool = False,
    ) -> None:
        """
        2. Sanitize TEMP (pós-clone, pré-dump).
        Com SANITIZE_SQL_PATH: executa o .sql.
        Sem path: lista schemas (information_schema; MySQL e PostgreSQL).

        Defaults (pipeline / CLI): até 2 tentativas (retry 60s); na falha
        final apaga TEMP e aborta. O game usa max_attempts=1 e
        preserve_temp_on_failure=True para iterar no .sql sem apagar TEMP.
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts deve ser >= 1, got {max_attempts}")
        self._resolve_db_credentials()
        self._require_env("TEMP_IDENTIFIER", "DB_PASSWORD")

        sql_path: Path | None = None
        sql_statement: str | None = None
        if self.sanitize_sql_path:
            sql_path = Path(self.sanitize_sql_path)
            if not sql_path.is_file():
                raise RuntimeError(
                    f"SANITIZE_SQL_PATH não encontrado ou não é arquivo: "
                    f"{sql_path}"
                )
            mode_label = f"file={sql_path}"
        else:
            sql_statement = SANITIZE_LIST_SCHEMAS_SQL
            mode_label = "list-schemas (SANITIZE_SQL_PATH vazio)"
            logging.info(
                "SANITIZE_SQL_PATH vazio: listando schemas na TEMP "
                "(%s).",
                SANITIZE_LIST_SCHEMAS_SQL,
            )

        endpoint = self._cluster_endpoint(self.temp_id, role="TEMP")
        engine, port = self._cluster_engine_and_port(
            self.temp_id, role="TEMP"
        )
        logging.info(
            "Sanitize TEMP %s (%s:%s engine=%s) %s",
            self.temp_id,
            endpoint,
            port,
            engine,
            mode_label,
        )

        last_error: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                logging.info(
                    "Sanitize tentativa %s/%s...", attempt, max_attempts
                )
                self._run_sanitize_cli(
                    host=endpoint,
                    port=port,
                    engine=engine,
                    sql_path=sql_path,
                    sql_statement=sql_statement,
                )
                logging.info(
                    "Sanitize TEMP concluído (tentativa %s).", attempt
                )
                return
            except Exception as error:
                last_error = error
                if attempt < max_attempts:
                    logging.warning(
                        "Sanitize falhou (tentativa %s/%s): %s. "
                        "Aguardando %ss e retentando...",
                        attempt,
                        max_attempts,
                        error,
                        SANITIZE_RETRY_WAIT_SECONDS,
                    )
                    time.sleep(SANITIZE_RETRY_WAIT_SECONDS)
                    continue
                if preserve_temp_on_failure:
                    logging.error(
                        "Sanitize falhou (tentativa %s/%s): %s. "
                        "TEMP %s preservado para inspeção.",
                        attempt,
                        max_attempts,
                        error,
                        self.temp_id,
                    )
                else:
                    logging.error(
                        "Sanitize falhou (tentativa %s/%s): %s. "
                        "Apagando TEMP %s.",
                        attempt,
                        max_attempts,
                        error,
                        self.temp_id,
                    )
                    self._delete_cluster_and_instance(self.temp_id)
                if max_attempts > 1:
                    raise RuntimeError(
                        f"Sanitize TEMP falhou após retry: {error}"
                    ) from error
                raise RuntimeError(
                    f"Sanitize TEMP falhou: {error}"
                ) from error

        raise RuntimeError(f"Sanitize TEMP falhou: {last_error}")

    def dump_dev_schemas(self) -> Path:
        """3. Dump dos schemas DEV (mydumper ou pg_dump -Fc)."""
        self._prune_old_dump_runs()
        self._resolve_db_credentials()
        self._require_env(
            "PROD_IDENTIFIER",
            "DEV_IDENTIFIER",
            "TEMP_IDENTIFIER",
            "DB_PASSWORD",
        )
        family = self._assert_homogeneous_engines(
            (self.prod_id, "PROD"),
            (self.dev_id, "DEV"),
            (self.temp_id, "TEMP"),
        )
        schemas = self._resolve_dev_only_schemas()
        endpoint = self._cluster_endpoint(self.dev_id)
        user = self._mysql_user_for(self.dev_id, role="DEV")
        engine, port = self._cluster_engine_and_port(self.dev_id, role="DEV")
        dump_dir = self._ensure_run_dump_dir()
        (dump_dir / ".engine").write_text(engine + "\n", encoding="utf-8")
        logging.info(
            "Dump DEV %s (%s:%s user=%s engine=%s) schemas=%s → %s",
            self.dev_id,
            endpoint,
            port,
            user,
            engine,
            schemas,
            dump_dir,
        )
        if family == "postgres":
            server_major = self._pg_server_major(endpoint, port, user, engine)
            pg_dump_path, client_major = self._resolve_pg_client(
                "pg_dump", server_major
            )
            (dump_dir / ".pg_client_major").write_text(
                f"{client_major}\n", encoding="utf-8"
            )
            dbname = self._default_database(engine)
            env = os.environ.copy()
            env["PGPASSWORD"] = self.db_password
            for schema in schemas:
                schema_dir = dump_dir / schema
                schema_dir.mkdir(parents=True, exist_ok=True)
                out_file = schema_dir / f"{schema}.dump"
                cmd = [
                    pg_dump_path,
                    "-h",
                    endpoint,
                    "-p",
                    str(port),
                    "-U",
                    user,
                    "-d",
                    dbname,
                    "-n",
                    schema,
                    "-Fc",
                    "--no-owner",
                    "--no-privileges",
                    "-f",
                    str(out_file),
                ]
                logging.info(
                    "pg_dump major=%s (%s) schema=%s → %s",
                    client_major,
                    pg_dump_path,
                    schema,
                    out_file,
                )
                subprocess.run(cmd, check=True, env=env)
        else:
            if not shutil.which("mydumper"):
                raise RuntimeError("Cliente mydumper não encontrado no PATH.")
            for schema in schemas:
                schema_dir = dump_dir / schema
                schema_dir.mkdir(parents=True, exist_ok=True)
                cmd = [
                    "mydumper",
                    "-h",
                    endpoint,
                    "-P",
                    str(port),
                    "-u",
                    user,
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
        """4. Restore dump DEV → TEMP (myloader ou pg_restore)."""
        self._resolve_db_credentials()
        self._require_env(
            "PROD_IDENTIFIER",
            "DEV_IDENTIFIER",
            "TEMP_IDENTIFIER",
            "DB_PASSWORD",
        )
        family = self._assert_homogeneous_engines(
            (self.prod_id, "PROD"),
            (self.dev_id, "DEV"),
            (self.temp_id, "TEMP"),
        )
        schemas = self._resolve_dev_only_schemas()
        endpoint = self._cluster_endpoint(self.temp_id)
        user = self._mysql_user_for(self.temp_id, role="TEMP")
        engine, port = self._cluster_engine_and_port(self.temp_id, role="TEMP")
        dump_dir = self._resolve_run_dump_dir()
        logging.info(
            "Restore dump %s → TEMP %s (%s:%s user=%s engine=%s) schemas=%s",
            dump_dir,
            self.temp_id,
            endpoint,
            port,
            user,
            engine,
            schemas,
        )
        if family == "postgres":
            temp_major = self._pg_server_major(endpoint, port, user, engine)
            dump_major = self._read_dump_pg_client_major(dump_dir)
            required_major = temp_major
            if dump_major is not None:
                required_major = max(temp_major, dump_major)
            pg_restore_path, client_major = self._resolve_pg_client(
                "pg_restore", required_major
            )
            dbname = self._default_database(engine)
            env = os.environ.copy()
            env["PGPASSWORD"] = self.db_password
            for schema in schemas:
                schema_dir = dump_dir / schema
                dump_file = schema_dir / f"{schema}.dump"
                if not dump_file.is_file():
                    raise RuntimeError(
                        f"Arquivo de dump PG ausente para schema {schema}: "
                        f"{dump_file} (run YYYYMMDD_HHMMSS)"
                    )
                cmd = [
                    pg_restore_path,
                    "-h",
                    endpoint,
                    "-p",
                    str(port),
                    "-U",
                    user,
                    "-d",
                    dbname,
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-privileges",
                    "-j",
                    "4",
                    str(dump_file),
                ]
                logging.info(
                    "pg_restore major=%s (%s) schema=%s → TEMP "
                    "(temp=%s dump=%s required=%s)",
                    client_major,
                    pg_restore_path,
                    schema,
                    temp_major,
                    dump_major if dump_major is not None else "n/a",
                    required_major,
                )
                subprocess.run(cmd, check=True, env=env)
        else:
            if not shutil.which("myloader"):
                raise RuntimeError("Cliente myloader não encontrado no PATH.")
            for schema in schemas:
                schema_dir = dump_dir / schema
                if not schema_dir.is_dir():
                    raise RuntimeError(
                        f"Pasta de dump ausente para schema {schema}: "
                        f"{schema_dir} (run YYYYMMDD_HHMMSS)"
                    )
                cmd = [
                    "myloader",
                    "-h",
                    endpoint,
                    "-P",
                    str(port),
                    "-u",
                    user,
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
        5. Compara COUNT(*) DEV vs TEMP nos schemas dumpados.
        Em falha: apaga TEMP e aborta (DEV intacto).
        """
        self._resolve_db_credentials()
        self._require_env(
            "PROD_IDENTIFIER",
            "DEV_IDENTIFIER",
            "TEMP_IDENTIFIER",
            "DB_PASSWORD",
        )
        self._assert_homogeneous_engines(
            (self.prod_id, "PROD"),
            (self.dev_id, "DEV"),
            (self.temp_id, "TEMP"),
        )
        schemas = self._resolve_restored_schemas()
        dev_host = self._cluster_endpoint(self.dev_id)
        temp_host = self._cluster_endpoint(self.temp_id)
        dev_user = self._mysql_user_for(self.dev_id, role="DEV")
        temp_user = self._mysql_user_for(self.temp_id, role="TEMP")
        logging.info(
            "Validando row counts DEV (%s user=%s) vs TEMP (%s user=%s), "
            "schemas=%s, tolerância=%s",
            dev_host,
            dev_user,
            temp_host,
            temp_user,
            schemas,
            self.row_diff_tolerance,
        )
        try:
            dev_counts = self._table_row_counts(
                self.dev_id, schemas, user=dev_user, role="DEV"
            )
            temp_counts = self._table_row_counts(
                self.temp_id, schemas, user=temp_user, role="TEMP"
            )
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
            artifact = self._resolve_run_dump_dir() / "validate_divergences.txt"
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
        """6. Renomeia DEV → OLD (cluster + writer)."""
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
        """7. Renomeia TEMP → DEV (cluster + writer)."""
        self._require_env("TEMP_IDENTIFIER", "DEV_IDENTIFIER", "PROD_IDENTIFIER")
        self._assert_not_prod(self.temp_id, "rename_temp_to_dev")
        self._assert_not_prod(self.dev_id, "rename_temp_to_dev")
        self._rename_cluster_and_instance(self.temp_id, self.dev_id)
        logging.info("Rename TEMP→DEV concluído: %s", self.dev_id)

    def delete_old_dev(self) -> None:
        """8. Apaga OLD (ou só loga se KEEP_OLD_DEV_HOURS > 0)."""
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
        """Executa passos 1→8 na ordem; archive S3 no finally se S3_BUCKET."""
        self._prune_old_dump_runs()
        self.start_run_file_log()
        try:
            logging.info("=== Pipeline Aurora Clone: início ===")
            self._resolve_db_credentials()
            # TEMP ainda não existe no início; o passo 1 cria o cluster.
            family = self._assert_homogeneous_engines(
                (self.prod_id, "PROD"),
                (self.dev_id, "DEV"),
            )
            logging.info("Pipeline engine family=%s", family)
            self.clone_prod_to_temp()
            self.sanitize_temp_data()
            self.dump_dev_schemas()
            self.restore_dump_to_temp()
            self.validate_dev_vs_temp()
            self.rename_dev_to_old()
            self.rename_temp_to_dev()
            self.delete_old_dev()
            logging.info("=== Pipeline Aurora Clone: concluída ===")
        finally:
            self.stop_run_file_log()
            self.archive_run_to_s3()
            self.release_run_dump_dir()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aurora Clone — pipeline PROD→DEV (passos 1–8)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "account-aws",
        help="Mostra Account/Arn AWS em uso (account mascarado nos logs)",
    )
    sub.add_parser("list-rds", help="Lista clusters RDS e engines")
    sub.add_parser("clone-prod-to-temp", help="1. Clone PROD → TEMP")
    sub.add_parser(
        "sanitize-temp",
        help=(
            "2. Sanitize TEMP (.sql via SANITIZE_SQL_PATH; "
            "vazio = lista schemas)"
        ),
    )
    sub.add_parser(
        "dump-dev",
        help="3. Dump schemas DEV (mydumper ou pg_dump -Fc)",
    )
    sub.add_parser(
        "restore-temp",
        help="4. Restore dump → TEMP (myloader ou pg_restore)",
    )
    sub.add_parser("validate", help="5. Valida DEV vs TEMP (row counts)")
    sub.add_parser("rename-dev-to-old", help="6. Rename DEV → OLD")
    sub.add_parser("rename-temp-to-dev", help="7. Rename TEMP → DEV")
    sub.add_parser("delete-old-dev", help="8. Delete OLD")
    sub.add_parser("pipeline", help="Roda passos 1→8")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = AuroraClone()
    command = args.command or "pipeline"

    if command == "account-aws":
        app.log_caller_identity()
    elif command == "list-rds":
        clusters = app.list_rds()
        logging.info(
            "Listagem concluída. Região: %s. Total: %s cluster(s).",
            app.region,
            len(clusters),
        )
    elif command == "clone-prod-to-temp":
        app.clone_prod_to_temp()
    elif command == "sanitize-temp":
        app.sanitize_temp_data()
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
