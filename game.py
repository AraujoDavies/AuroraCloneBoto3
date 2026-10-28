"""
Operação Aurora — campanha narrativa no terminal (MVP).

Três fases, avanço 100% manual (hub → Enter inicia etapa → volta ao hub).

  01 Reconhecimento — pré-voo (tools, AWS, RDS, policy, senha; .env sob demanda)
  02 Escalada       — clone → dump → restore → validate → renames
  03 Aurora         — delete OLD (+ animação de espera)

Uso (container -it):
  make PYTHON=python game
  python game.py --from shield --no-anim

Hack (dev): AURORA_GAME_HACK=1 python game.py
"""

from __future__ import annotations

import argparse
import curses
import json
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from botocore.exceptions import ClientError
from dotenv import load_dotenv

from aurora_clone import AuroraClone, mask_account_id, mask_account_in_text

CHECKPOINT_PATH = Path(".aurora_game_checkpoint")
NO_ANIM = False
# Etapa removida — filtrar de checkpoints antigos.
_OBSOLETE_STAGE_IDS = frozenset({"dotenv"})

# ---------------------------------------------------------------------------
# ANSI / TTY
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"


def c(text: str, *codes: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{''.join(codes)}{text}{RESET}"


def clear_screen() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def sleep(seconds: float) -> None:
    if NO_ANIM:
        return
    time.sleep(seconds)


def pause(prompt: str = "  [ Enter ] ") -> None:
    try:
        input(c(prompt, DIM))
    except EOFError:
        print()


def write(line: str = "") -> None:
    print(line, flush=True)


def require_tty() -> None:
    if sys.stdout.isatty() and sys.stdin.isatty():
        return
    write(
        "Operação Aurora precisa de um terminal interativo (-it).\n"
        "Exemplo:\n"
        "  docker run --rm -it --user \"$(id -u):$(id -g)\" "
        "$([ -f .env ] && echo --env-file .env) "
        "-v \"$(pwd):/app\" -w /app aurora-clone\n"
        "  make PYTHON=python game"
    )
    raise SystemExit(2)


def _hack_enabled() -> bool:
    raw = (os.getenv("AURORA_GAME_HACK") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


def load_checkpoint() -> dict:
    if not CHECKPOINT_PATH.is_file():
        return {"phase_id": None, "cleared": [], "updated_at": None}
    try:
        data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"phase_id": None, "cleared": [], "updated_at": None}
    cleared = data.get("cleared") or []
    if not isinstance(cleared, list):
        cleared = []
    cleared_ids = [
        str(x) for x in cleared if str(x) not in _OBSOLETE_STAGE_IDS
    ]
    phase_id = data.get("phase_id")
    if phase_id in _OBSOLETE_STAGE_IDS:
        phase_id = cleared_ids[-1] if cleared_ids else None
    return {
        "phase_id": phase_id,
        "cleared": cleared_ids,
        "updated_at": data.get("updated_at"),
    }


def save_checkpoint(phase_id: str, cleared: list[str]) -> None:
    payload = {
        "phase_id": phase_id,
        "cleared": cleared,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    CHECKPOINT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def reset_checkpoint() -> None:
    """Apaga o checkpoint e desmarca todas as etapas."""
    try:
        CHECKPOINT_PATH.unlink(missing_ok=True)
    except OSError as error:
        raise RuntimeError(f"Não foi possível apagar o checkpoint: {error}") from error


# ---------------------------------------------------------------------------
# Acts / stages model
# ---------------------------------------------------------------------------

ACT_RECON = "recon"
ACT_CLIMB = "climb"
ACT_AURORA = "aurora"
CLEAR_ID = "mission_clear"

ACT_META = {
    ACT_RECON: {
        "num": "01",
        "title": "RECONHECIMENTO",
        "phrase": "Toda expedição começa antes de cruzar as muralhas.",
        "banner": CYAN,
    },
    ACT_CLIMB: {
        "num": "02",
        "title": "ESCALADA",
        "phrase": "As muralhas cederam. Agora, não há caminho de volta.",
        "banner": RED,
    },
    ACT_AURORA: {
        "num": "03",
        "title": "AURORA",
        "phrase": "O silêncio precede o amanhecer.",
        "banner": MAGENTA,
    },
}


@dataclass
class Stage:
    id: str
    act: str
    title: str
    mission: str
    hints: list[str]
    run: Callable[[AuroraClone | None], str | None]
    clear_message: str | None = None
    needs_app: bool = False
    mutates: bool = False
    confirm_label: str | None = None
    sample_errors: list[str] = field(default_factory=list)


# --- Env sob demanda (reload a cada jogada) ---


def _reload_dotenv() -> None:
    """Recarrega .env do cwd; override vence strings vazias do --env-file.

    Credenciais AWS passam a valer na próxima instância de AuroraClone (retry).
    """
    env_path = Path(".env")
    example_path = Path(".env.example")
    if not env_path.is_file() and example_path.is_file():
        shutil.copyfile(example_path, env_path)
        write(c("  [ .env ] criado a partir de .env.example", DIM))
    load_dotenv(override=True)


def _require_env_vars(
    *keys: str,
    either_of: tuple[str, ...] | None = None,
) -> None:
    """Falha cedo listando variáveis ausentes/vazias no .env."""
    missing: list[str] = []
    for key in keys:
        if not (os.getenv(key) or "").strip():
            missing.append(key)
    if either_of is not None:
        if not any((os.getenv(k) or "").strip() for k in either_of):
            missing.append("|".join(either_of))
    if missing:
        raise RuntimeError(
            "Faltam variáveis de ambiente no .env: "
            + ", ".join(missing)
            + ". Edite o .env e use [r] retry (sem reiniciar o container)."
        )


def _require_aws_env() -> None:
    """Credenciais + região AWS (AWS_REGION aceito no lugar de DEFAULT)."""
    missing: list[str] = []
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not (os.getenv(key) or "").strip():
            missing.append(key)
    if not (
        (os.getenv("AWS_DEFAULT_REGION") or "").strip()
        or (os.getenv("AWS_REGION") or "").strip()
    ):
        missing.append("AWS_DEFAULT_REGION")
    if missing:
        raise RuntimeError(
            "Faltam variáveis de ambiente no .env: "
            + ", ".join(missing)
            + ". Edite o .env e use [r] retry (sem reiniciar o container)."
        )


# --- Reconhecimento checks (pré-voo) ---


def _check_toolbox(_app: AuroraClone | None) -> str | None:
    required = ("mydumper", "myloader", "terraform", "make")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            "Ferramentas ausentes no PATH: " + ", ".join(missing)
        )
    write(c("  [ OK ] Ferramentas no PATH:", GREEN))
    for name in required:
        write(c(f"         {name} → {shutil.which(name)}", DIM))
    return None


def _check_whoami(app: AuroraClone | None) -> str | None:
    assert app is not None
    _require_aws_env()
    identity = app.log_caller_identity()
    account = identity.get("Account")
    arn = identity.get("Arn")
    if not account or not arn:
        raise RuntimeError("STS retornou identity incompleta (Account/Arn).")
    write(c(f"  [ OK ] Account: {mask_account_id(account)}", GREEN))
    write(c(f"         Arn:     {mask_account_in_text(arn, account)}", DIM))
    write(c(f"         Região:  {app.region}", DIM))
    return (
        f"Identidade arn={mask_account_in_text(arn, account)} "
        f"({app.region or '?'})"
    )


def _check_shield(app: AuroraClone | None) -> str | None:
    assert app is not None
    _require_aws_env()
    result = app.check_avoid_delete_policy()
    if not result.get("exists"):
        raise RuntimeError(
            f"Policy '{result.get('policy_name')}' NÃO encontrada na conta."
        )
    if not result.get("active"):
        raise RuntimeError(
            f"Policy '{result.get('policy_name')}' existe mas NÃO está "
            "anexada à identity atual (active=False)."
        )
    account = (app.sts.get_caller_identity().get("Account") or "").strip()
    write(c(f"  [ OK ] Policy: {result.get('policy_name')}", GREEN))
    write(
        c(
            f"         ARN: {mask_account_in_text(result.get('policy_arn'), account)}",
            DIM,
        )
    )
    return None


def _cluster_status(app: AuroraClone, identifier: str) -> str | None:
    if not app._cluster_exists(identifier):
        return None
    response = app.rds.describe_db_clusters(DBClusterIdentifier=identifier)
    clusters = response.get("DBClusters") or []
    if not clusters:
        return None
    return clusters[0].get("Status")


def _radar_env_mismatch_error(
    app: AuroraClone,
    clusters: list[dict],
    missing_labels: list[str],
) -> RuntimeError:
    """Bancos existem na região, mas DEV_IDENTIFIER do .env não bate."""
    lines = [
        "Os bancos existem na região, mas as variáveis de ambiente "
        "não correspondem.",
        f"Região: {app.region}",
        f".env atual: DEV_IDENTIFIER={app.dev_id or '(vazio)'}",
        f"Não encontrados no .env: {', '.join(missing_labels)}",
        "Clusters encontrados na região:",
    ]
    for item in clusters:
        ident = item.get("identifier") or "?"
        lines.append(
            f"  - {c(str(ident), GREEN, BOLD)}  status={item.get('status')}"
        )
    lines.append(
        "Edite o .env (DEV_IDENTIFIER) com o valor correspondente "
        "e use [r] retry."
    )
    return RuntimeError("\n".join(lines))


def _check_radar(app: AuroraClone | None) -> str | None:
    assert app is not None
    clusters = app.list_rds()
    if not clusters:
        raise RuntimeError(
            "Banco não existe. Crie (Terraform — ver README) para "
            "prosseguir, ou altere a região."
        )

    by_id = {item.get("identifier"): item for item in clusters}
    env_mismatches: list[str] = []
    status_problems: list[str] = []

    for label, cid in (("DEV", app.dev_id),):
        if not cid:
            env_mismatches.append(label)
            continue
        item = by_id.get(cid)
        status = item.get("status") if item else _cluster_status(app, cid)
        if status is None:
            env_mismatches.append(label)
            continue
        if status != "available":
            status_problems.append(
                f"{label} ({cid}) status={status!r} (esperado available)"
            )
            continue
        write(c(f"  [ OK ] {label} {cid}: available", GREEN))
        if item and item.get("endpoint"):
            write(c(f"         endpoint: {item.get('endpoint')}", DIM))

    if env_mismatches:
        raise _radar_env_mismatch_error(app, clusters, env_mismatches)
    if status_problems:
        raise RuntimeError("; ".join(status_problems))

    _require_env_vars("TEMP_IDENTIFIER")

    for label, cid in (("TEMP", app.temp_id), ("OLD", app.old_id)):
        if not cid:
            continue
        status = _cluster_status(app, cid)
        if status is not None:
            write(
                c(
                    f"  [WARN] {label} ({cid}) já existe (status={status})",
                    YELLOW,
                )
            )
        else:
            write(c(f"  [ OK ] {label}: ausente (ok para primeiro clone)", DIM))
    return None


def _check_keys(app: AuroraClone | None) -> str | None:
    assert app is not None
    _require_env_vars(either_of=("DB_PASSWORD", "DB_PASSWORD_SSM_PARAM"))
    from_env = bool((app.db_password or "").strip()) or bool(
        (os.getenv("DB_PASSWORD") or "").strip()
    )
    info = app.verify_dev_login()
    fonte = ".env" if from_env else "SSM"
    write(c("  [ OK ] DB_PASSWORD resolvida e login DEV ok", GREEN))
    write(
        c(
            f"         fonte: {fonte}  |  comprimento: {len(app.db_password)} chars",
            DIM,
        )
    )
    write(
        c(
            f"         user={info['user']}  host={info['host']}  "
            f"cluster={info['cluster']}",
            DIM,
        )
    )
    return f"Login DEV ok ({info['user']}@{info['host']}, senha via {fonte})"


def _check_pipe(app: AuroraClone | None) -> str | None:
    write(c("  Inventário final do Reconhecimento:", CYAN, BOLD))
    for line in (
        "tools",
        "AWS STS",
        "RDS radar",
        "IAM shield",
        "DB keys",
    ):
        write(c(f"    [ OK ] {line}", GREEN))
    if app is not None:
        write(
            c(
                f"    região={app.region}  PROD={app.prod_id}  DEV={app.dev_id}",
                DIM,
            )
        )
    write()
    write(
        c(
            "  Reconhecimento concluído. Inicie a Escalada no hub quando quiser.",
            YELLOW,
            BOLD,
        )
    )
    return None


# --- Escalada / Aurora (mutações reais via AuroraClone) ---


def _show_targets(app: AuroraClone) -> None:
    write(c("  Alvos (sem segredos):", WHITE, BOLD))
    write(c(f"    PROD = {app.prod_id}", DIM))
    write(c(f"    DEV  = {app.dev_id}", DIM))
    write(c(f"    TEMP = {app.temp_id}", DIM))
    write(c(f"    OLD  = {app.old_id}", DIM))
    write(c(f"    região = {app.region}", DIM))


def _run_clone(app: AuroraClone | None) -> str | None:
    assert app is not None
    _show_targets(app)
    app._resolve_db_credentials()
    write(c("  [RUN] clone PROD → TEMP...", YELLOW))
    app.clone_prod_to_temp()
    write(c("  [ OK ] Clone concluído", GREEN))
    return f"TEMP criado: {app.temp_id}"


def _run_dump(app: AuroraClone | None) -> str | None:
    assert app is not None
    _show_targets(app)
    app._resolve_db_credentials()
    write(c("  [RUN] dump schemas DEV...", YELLOW))
    path = app.dump_dev_schemas()
    write(c(f"  [ OK ] Dump em {path}", GREEN))
    return f"Dump gravado em {path}"


def _run_restore(app: AuroraClone | None) -> str | None:
    assert app is not None
    _show_targets(app)
    app._resolve_db_credentials()
    write(c("  [RUN] restore dump → TEMP...", YELLOW))
    app.restore_dump_to_temp()
    write(c("  [ OK ] Restore concluído", GREEN))
    return None


def _run_validate(app: AuroraClone | None) -> str | None:
    assert app is not None
    _show_targets(app)
    app._resolve_db_credentials()
    write(c("  [RUN] validate DEV vs TEMP...", YELLOW))
    result = app.validate_dev_vs_temp()
    write(c(f"  [ OK ] Validate: {result}", GREEN))
    return "Contagens dentro da tolerância"


def _run_rename_old(app: AuroraClone | None) -> str | None:
    assert app is not None
    _show_targets(app)
    write(c("  [RUN] rename DEV → OLD...", YELLOW))
    app.rename_dev_to_old()
    write(c("  [ OK ] DEV renomeado para OLD", GREEN))
    return f"OLD = {app.old_id}"


def _run_rename_dev(app: AuroraClone | None) -> str | None:
    assert app is not None
    _show_targets(app)
    write(c("  [RUN] rename TEMP → DEV...", YELLOW))
    app.rename_temp_to_dev()
    write(c("  [ OK ] TEMP virou DEV (cutover)", GREEN))
    return f"Novo DEV = {app.dev_id}"


def _run_delete_old(app: AuroraClone | None) -> str | None:
    """Chamado após animação/thread — ou direto no hack pass."""
    assert app is not None
    _show_targets(app)
    keep = app.keep_old_dev_hours
    if keep > 0:
        write(
            c(
                f"  [WARN] KEEP_OLD_DEV_HOURS={keep} > 0 — NÃO apagará OLD",
                YELLOW,
                BOLD,
            )
        )
    write(c("  [RUN] delete OLD...", YELLOW))
    app.delete_old_dev()
    if keep > 0:
        write(c("  [ OK ] Hold respeitado — OLD preservado", YELLOW))
        return f"OLD preservado (KEEP_OLD_DEV_HOURS={keep})"
    write(c("  [ OK ] OLD removido", GREEN))
    return f"OLD removido: {app.old_id}"


STAGES: list[Stage] = [
    Stage(
        id="toolbox",
        act=ACT_RECON,
        title="Toolbox",
        mission="Conferir mydumper, myloader, terraform e make no PATH.",
        hints=[
            "Siga o comando docker run completo no inicio do arquivo README.md",
        ],
        run=_check_toolbox,
        clear_message="Ferramentas disponíveis",
        sample_errors=["Ferramentas ausentes no PATH: mydumper, myloader"],
    ),
    Stage(
        id="whoami",
        act=ACT_RECON,
        title="WhoAmI",
        mission="Confirmar credenciais AWS via STS.",
        hints=[
            "Preencha AWS_ACCESS_KEY_ID e AWS_SECRET_ACCESS_KEY no .env.",
            "Defina AWS_DEFAULT_REGION (ou AWS_REGION).",
            "Salve o .env e use [r] retry — sem reiniciar o container.",
            "Teste: make PYTHON=python account-aws",
        ],
        run=_check_whoami,
        needs_app=True,
        sample_errors=[
            "Faltam variáveis de ambiente no .env: AWS_ACCESS_KEY_ID, "
            "AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION. Edite o .env e use "
            "[r] retry (sem reiniciar o container).",
            "Credenciais AWS inválidas: o Secret Access Key não confere com o "
            "Access Key ID (valor errado, truncado ou com aspas/espaço extra no .env).",
        ],
    ),
    Stage(
        id="radar",
        act=ACT_RECON,
        title="Radar",
        mission="DEV existe e está available na região; TEMP/OLD só avisam.",
        hints=[
            "Se não houver cluster na região: suba com Terraform "
            "(README) ou altere AWS_DEFAULT_REGION no .env.",
            "Se os bancos existirem: ajuste DEV_IDENTIFIER no .env "
            "para o identifier listado.",
            "Preencha também TEMP_IDENTIFIER; salve e use [r] retry.",
            "Teste: make PYTHON=python list-rds",
        ],
        run=_check_radar,
        needs_app=True,
        clear_message="Cluster DEV available",
        sample_errors=[
            "Banco não existe. Crie (Terraform — ver README) para "
            "prosseguir, ou altere a região.",
            "Os bancos existem na região, mas as variáveis de ambiente "
            "não correspondem. Edite o .env (DEV_IDENTIFIER) com o "
            "valor correspondente e use [r] retry.",
        ],
    ),
    Stage(
        id="shield",
        act=ACT_RECON,
        title="Shield",
        mission="Policy AvoidDeleteAuroraCloneBoto3 existe e está anexada.",
        hints=[
            "Siga role.md para criar/anexar a policy.",
            "Credenciais AWS no .env (mesmas do WhoAmI).",
            "Teste: make PYTHON=python check-policy",
        ],
        run=_check_shield,
        needs_app=True,
        clear_message="Policy de proteção da PROD ativa",
        sample_errors=[
            "Policy 'AvoidDeleteAuroraCloneBoto3' NÃO encontrada na conta.",
            "Faltam variáveis de ambiente no .env: AWS_ACCESS_KEY_ID, "
            "AWS_SECRET_ACCESS_KEY. Edite o .env e use [r] retry "
            "(sem reiniciar o container).",
        ],
    ),
    Stage(
        id="keys",
        act=ACT_RECON,
        title="Keys",
        mission="Resolver senha e validar login MySQL no cluster DEV.",
        hints=[
            "Preencha DB_PASSWORD no .env, ou DB_PASSWORD_SSM_PARAM.",
            "Se MasterUsername DEV ≠ PROD, defina DB_USER no .env.",
            "Timeout/rede: confira SG e se o DEV é alcançável do container.",
            "Salve e use [r] retry — sem reiniciar o container.",
        ],
        run=_check_keys,
        needs_app=True,
        sample_errors=[
            "Faltam variáveis de ambiente no .env: "
            "DB_PASSWORD|DB_PASSWORD_SSM_PARAM. Edite o .env e use [r] retry "
            "(sem reiniciar o container).",
            "Falha ao conectar no DEV (dev-aurora @ …) como 'admin': "
            "(1045, Access denied). Confira DB_USER/DB_PASSWORD e rede/SG.",
        ],
    ),
    Stage(
        id="pipe",
        act=ACT_RECON,
        title="PipeReady",
        mission="Inventário final — libera a Escalada no hub.",
        hints=["Inicie a Escalada no mapa quando estiver pronto."],
        run=_check_pipe,
        needs_app=True,
        clear_message="Reconhecimento completo",
    ),
    Stage(
        id="clone",
        act=ACT_CLIMB,
        title="Clone",
        mission="Clone PROD → TEMP (copy-on-write + writer).",
        hints=[
            "Equivalente a: make PYTHON=python clone-prod-to-temp",
            "Confira PROD_IDENTIFIER no .env (cluster fonte do clone).",
            "Pode demorar até o cluster/instância ficarem available.",
        ],
        run=_run_clone,
        needs_app=True,
        mutates=True,
        confirm_label="clone-prod-to-temp",
        clear_message="TEMP criado a partir de PROD",
        sample_errors=[
            "TEMP já existe",
            "Cluster do banco de dados de PROD (prod-aurora) "
            "não foi encontrado! Confira PROD_IDENTIFIER no .env "
            "e se o cluster existe na região.",
            "Timeout aguardando available",
        ],
    ),
    Stage(
        id="dump",
        act=ACT_CLIMB,
        title="Dump",
        mission="Dump dos schemas exclusivos de DEV.",
        hints=["Equivalente a: make PYTHON=python dump-dev"],
        run=_run_dump,
        needs_app=True,
        mutates=True,
        confirm_label="dump-dev",
        clear_message="Dump DEV concluído",
        sample_errors=["TEMP obrigatório para resolver schemas DEV−TEMP"],
    ),
    Stage(
        id="restore",
        act=ACT_CLIMB,
        title="Restore",
        mission="Restore do dump no TEMP (myloader).",
        hints=["Equivalente a: make PYTHON=python restore-temp"],
        run=_run_restore,
        needs_app=True,
        mutates=True,
        confirm_label="restore-temp",
        clear_message="Restore no TEMP concluído",
        sample_errors=["Pasta de dump do dia não encontrada"],
    ),
    Stage(
        id="validate",
        act=ACT_CLIMB,
        title="Validate",
        mission="Validar contagens DEV vs TEMP.",
        hints=[
            "Equivalente a: make PYTHON=python validate",
            "Se falhar, TEMP é apagado e DEV permanece intacto.",
        ],
        run=_run_validate,
        needs_app=True,
        mutates=True,
        confirm_label="validate",
        clear_message="Validate OK",
        sample_errors=["Divergência acima de ROW_DIFF_TOLERANCE"],
    ),
    Stage(
        id="rename_old",
        act=ACT_CLIMB,
        title="RenameOld",
        mission="Rename DEV → OLD.",
        hints=["Equivalente a: make PYTHON=python rename-dev-to-old"],
        run=_run_rename_old,
        needs_app=True,
        mutates=True,
        confirm_label="rename-dev-to-old",
        clear_message="DEV renomeado para OLD",
        sample_errors=["OLD já existe"],
    ),
    Stage(
        id="rename_dev",
        act=ACT_CLIMB,
        title="RenameDev",
        mission="Rename TEMP → DEV (cutover).",
        hints=["Equivalente a: make PYTHON=python rename-temp-to-dev"],
        run=_run_rename_dev,
        needs_app=True,
        mutates=True,
        confirm_label="rename-temp-to-dev",
        clear_message="Cutover concluído — TEMP virou DEV",
        sample_errors=["Cluster TEMP não encontrado"],
    ),
    Stage(
        id="delete_old",
        act=ACT_AURORA,
        title="DeleteOld",
        mission="Apagar OLD (vestígios). Respeita KEEP_OLD_DEV_HOURS.",
        hints=[
            "Equivalente a: make PYTHON=python delete-old-dev",
        ],
        run=_run_delete_old,
        needs_app=True,
        mutates=True,
        confirm_label="delete-old-dev",
        clear_message="Vestígios tratados",
        sample_errors=["OLD não encontrado", "AccessDenied em delete"],
    ),
]

STAGE_BY_ID = {s.id: s for s in STAGES}
PATH_ORDER = [s.id for s in STAGES] + [CLEAR_ID]


def stage_index(stage_id: str) -> int:
    for i, stage in enumerate(STAGES):
        if stage.id == stage_id:
            return i
    raise KeyError(stage_id)


def active_act(cleared: set[str]) -> str:
    """Fase da fronteira: primeira etapa ainda não cleared (ou Aurora se 100%)."""
    for stage in STAGES:
        if stage.id not in cleared:
            return stage.act
    return ACT_AURORA


def unlocked_ids(cleared: set[str], hack_on: bool) -> list[str]:
    """Cleared + próxima; HACK libera todas as etapas. CLEAR se 100%."""
    if hack_on:
        out = [s.id for s in STAGES]
    else:
        out = []
        for stage in STAGES:
            out.append(stage.id)
            if stage.id not in cleared:
                break
    if {s.id for s in STAGES} <= set(cleared):
        out.append(CLEAR_ID)
    return out


def default_cursor(
    cleared: set[str],
    hack_on: bool,
    prefer: str | None = None,
) -> str:
    unlocked = unlocked_ids(cleared, hack_on)
    if prefer and prefer in unlocked:
        return prefer
    for stage in STAGES:
        if stage.id not in cleared and stage.id in unlocked:
            return stage.id
    if CLEAR_ID in unlocked:
        return CLEAR_ID
    return unlocked[0] if unlocked else STAGES[0].id


def move_cursor(cursor: str, direction: str, unlocked: set[str]) -> str:
    order = [nid for nid in PATH_ORDER if nid in unlocked]
    if not order:
        return cursor
    if cursor not in order:
        return order[0]
    i = order.index(cursor)
    if direction == "next":
        return order[min(i + 1, len(order) - 1)]
    return order[max(i - 1, 0)]


DIR_FROM_KEY = {
    curses.KEY_LEFT: "prev",
    curses.KEY_RIGHT: "next",
    curses.KEY_UP: "prev",
    curses.KEY_DOWN: "next",
}


# ---------------------------------------------------------------------------
# Hub (curses)
# ---------------------------------------------------------------------------


def _curses_init_colors(act: str = ACT_RECON) -> dict[str, int]:
    pairs: dict[str, int] = {
        "title": 0,
        "dim": 0,
        "clear": 0,
        "cursor": 0,
        "locked": 0,
        "open": 0,
        "hint": 0,
        "border": 0,
        "hack": 0,
        "act": 0,
        "act_recon": 0,
        "act_climb": 0,
        "act_aurora": 0,
        "warn": 0,
    }
    if not curses.has_colors():
        pairs["cursor"] = curses.A_REVERSE | curses.A_BOLD
        return pairs
    curses.start_color()
    bg = curses.COLOR_BLACK
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    # 1=yellow 2=cyan 3=green 4=magenta 5=white 6=blue 8=red
    # 7/9/10 = cursor invertidos por fase
    curses.init_pair(1, curses.COLOR_YELLOW, bg)
    curses.init_pair(2, curses.COLOR_CYAN, bg)
    curses.init_pair(3, curses.COLOR_GREEN, bg)
    curses.init_pair(4, curses.COLOR_MAGENTA, bg)
    curses.init_pair(5, curses.COLOR_WHITE, bg)
    curses.init_pair(6, curses.COLOR_BLUE, bg)
    curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(8, curses.COLOR_RED, bg)
    curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_RED)
    curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_MAGENTA)

    theme_chrome = {
        ACT_RECON: (curses.color_pair(1), curses.color_pair(7), curses.color_pair(2)),
        ACT_CLIMB: (curses.color_pair(8), curses.color_pair(9), curses.color_pair(8)),
        ACT_AURORA: (curses.color_pair(4), curses.color_pair(10), curses.color_pair(4)),
    }
    chrome, cursor, act_fg = theme_chrome.get(
        act, theme_chrome[ACT_RECON]
    )

    pairs["title"] = chrome | curses.A_BOLD
    pairs["border"] = chrome
    pairs["dim"] = curses.A_DIM
    pairs["clear"] = curses.color_pair(3) | curses.A_BOLD
    pairs["cursor"] = cursor | curses.A_BOLD
    pairs["locked"] = curses.A_DIM
    pairs["open"] = curses.color_pair(5) | curses.A_BOLD
    pairs["hint"] = curses.A_DIM
    pairs["hack"] = curses.color_pair(4) | curses.A_BOLD
    pairs["act"] = act_fg | curses.A_BOLD
    pairs["act_recon"] = curses.color_pair(2) | curses.A_BOLD
    pairs["act_climb"] = curses.color_pair(8) | curses.A_BOLD
    pairs["act_aurora"] = curses.color_pair(4) | curses.A_BOLD
    pairs["warn"] = curses.color_pair(8) | curses.A_BOLD
    return pairs


def _addstr(stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        max_y, max_x = stdscr.getmaxyx()
        if y < 0 or y >= max_y or x >= max_x:
            return
        room = max_x - x - 1
        if room <= 0:
            return
        stdscr.addstr(y, x, text[:room], attr)
    except curses.error:
        pass


def _stage_mark(stage_id: str, cleared: set[str], unlocked: set[str]) -> str:
    if stage_id == CLEAR_ID:
        return "▲" if stage_id in unlocked else "·"
    if stage_id in cleared:
        return "★"
    if stage_id in unlocked:
        return "○"
    return "·"


def _draw_hub(
    stdscr: curses.window,
    cleared: set[str],
    cursor: str,
    unlocked: set[str],
    colors: dict[str, int],
    hack_on: bool,
) -> None:
    stdscr.erase()
    y = 0
    _addstr(stdscr, y, 0, "  ╔" + "═" * 58 + "╗", colors["border"])
    y += 1
    _addstr(
        stdscr,
        y,
        0,
        "  ║" + "  O P E R A Ç Ã O   A U R O R A".center(58) + "║",
        colors["title"],
    )
    y += 1
    _addstr(
        stdscr,
        y,
        0,
        "  ║" + "  hub · setas navegam · Enter inicia etapa".center(58) + "║",
        colors["dim"],
    )
    y += 1
    _addstr(stdscr, y, 0, "  ╠" + "═" * 58 + "╣", colors["border"])
    y += 1

    frontier = active_act(cleared)
    act_color_key = {
        ACT_RECON: "act_recon",
        ACT_CLIMB: "act_climb",
        ACT_AURORA: "act_aurora",
    }
    current_act = None
    for stage in STAGES:
        if stage.act != current_act:
            current_act = stage.act
            meta = ACT_META[current_act]
            act_unlocked = any(s.id in unlocked for s in STAGES if s.act == current_act)
            label = f"  [{meta['num']}] {meta['title']}"
            if not act_unlocked:
                attr = colors["locked"]
            elif current_act == frontier:
                attr = colors["act"]
            else:
                attr = colors.get(act_color_key[current_act], colors["act"])
            _addstr(stdscr, y, 0, "  ║", colors["border"])
            _addstr(stdscr, y, 3, label.ljust(56), attr)
            _addstr(stdscr, y, 61, "║", colors["border"])
            y += 1

        mark = _stage_mark(stage.id, cleared, unlocked)
        mutate = "!" if stage.mutates else " "
        line = f"    {mark}{mutate} {stage.title}"
        if stage.id == cursor:
            attr = colors["cursor"]
        elif stage.id in cleared:
            attr = colors["clear"]
        elif stage.id in unlocked:
            attr = colors["open"]
        else:
            attr = colors["locked"]
        _addstr(stdscr, y, 0, "  ║", colors["border"])
        _addstr(stdscr, y, 3, line.ljust(56), attr)
        _addstr(stdscr, y, 61, "║", colors["border"])
        y += 1

    # mission clear node
    mark = _stage_mark(CLEAR_ID, cleared, unlocked)
    line = f"    {mark}  MISSÃO CONCLUÍDA"
    if CLEAR_ID == cursor:
        attr = colors["cursor"]
    elif CLEAR_ID in unlocked:
        attr = colors["clear"]
    else:
        attr = colors["locked"]
    _addstr(stdscr, y, 0, "  ║", colors["border"])
    _addstr(stdscr, y, 3, line.ljust(56), attr)
    _addstr(stdscr, y, 61, "║", colors["border"])
    y += 1

    _addstr(stdscr, y, 0, "  ║" + " ".ljust(58) + "║", colors["dim"])
    y += 1
    _addstr(
        stdscr,
        y,
        0,
        "  ║"
        + "  ★ clear  ○ próximo  · locked  ! mutação  ▲ vitória".ljust(58)
        + "║",
        colors["hint"],
    )
    y += 1
    _addstr(stdscr, y, 0, "  ╚" + "═" * 58 + "╝", colors["border"])
    y += 2

    _addstr(stdscr, y, 2, "← → navegar · Enter iniciar · q sair", colors["hint"])
    y += 1
    _addstr(
        stdscr,
        y,
        2,
        "Nunca avança sozinho — cada etapa começa no Enter.",
        colors["hint"],
    )
    y += 1
    if hack_on:
        _addstr(stdscr, y, 2, "HACK ON — todas as etapas liberadas", colors["hack"])
        y += 1

    if cursor == CLEAR_ID:
        sel = "MISSÃO CONCLUÍDA"
        detail = "Enter = tela de vitória"
    else:
        stage = STAGE_BY_ID[cursor]
        sel = f"[{ACT_META[stage.act]['num']}] {stage.title}"
        if cursor in cleared:
            detail = "concluída · Enter = rejogar"
        elif cursor in unlocked:
            detail = (
                "mutação · Enter = confirmar e executar"
                if stage.mutates
                else "próxima · Enter = iniciar"
            )
        else:
            detail = "bloqueada"
    _addstr(stdscr, y + 1, 2, f"> {sel}", colors["title"])
    _addstr(stdscr, y + 2, 2, detail, colors["hint"])
    stdscr.refresh()


def map_hub(
    cleared: set[str],
    cursor: str,
    hack_on: bool,
) -> tuple[str, str]:
    """Retorna (action, node_id): play|clear|quit."""
    result: dict[str, str] = {"action": "quit", "node": cursor}

    def _run(stdscr: curses.window) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(-1)
        cur = (
            cursor
            if cursor in unlocked_ids(cleared, hack_on)
            else default_cursor(cleared, hack_on)
        )
        while True:
            unlocked = unlocked_ids(cleared, hack_on)
            unlocked_set = set(unlocked)
            if cur not in unlocked_set:
                cur = default_cursor(cleared, hack_on)
            colors = _curses_init_colors(active_act(cleared))
            _draw_hub(stdscr, cleared, cur, unlocked_set, colors, hack_on)
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                result["action"] = "quit"
                result["node"] = cur
                return
            if key in (curses.KEY_ENTER, 10, 13):
                if cur not in unlocked_set:
                    curses.flash()
                    continue
                result["action"] = "clear" if cur == CLEAR_ID else "play"
                result["node"] = cur
                return
            if key in DIR_FROM_KEY:
                cur = move_cursor(cur, DIR_FROM_KEY[key], unlocked_set)

    curses.wrapper(_run)
    return result["action"], result["node"]


# ---------------------------------------------------------------------------
# Visual: clear / aurora / banners
# ---------------------------------------------------------------------------


def phase_clear(
    stage_name: str,
    clear_message: str | None = None,
    *,
    act: str = ACT_RECON,
) -> None:
    accent = ACT_META.get(act, ACT_META[ACT_RECON])["banner"]
    write()
    write(c("  ════════════════════════════", accent))
    write(c(f"  ETAPA CONCLUÍDA  »  {stage_name}", accent, BOLD))
    if clear_message:
        write(c(f"  {clear_message}", GREEN))
    if act == ACT_CLIMB and stage_name == "PipeReady":
        write(c("  → ESCALADA liberada. As coisas mudaram.", accent, BOLD))
    write(c("  Volte ao hub para iniciar a próxima (manual).", DIM))
    write(c("  ════════════════════════════", accent))
    write()
    sleep(0.25)


def title_banner() -> None:
    write(
        c(
            r"""
     ██████╗ ██████╗ ███████╗██████╗  █████╗  ██████╗ █████╗  ██████╗
    ██╔═══██╗██╔══██╗██╔════╝██╔══██╗██╔══██╗██╔════╝██╔══██╗██╔═══██╗
    ██║   ██║██████╔╝█████╗  ██████╔╝███████║██║     ███████║██║   ██║
    ██║   ██║██╔═══╝ ██╔══╝  ██╔══██╗██╔══██║██║     ██╔══██║██║   ██║
    ╚██████╔╝██║     ███████╗██║  ██║██║  ██║╚██████╗██║  ██║╚██████╔╝
     ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝ ╚═════╝
                    A U R O R A
""",
            CYAN,
            BOLD,
        )
    )


def _aurora_frames() -> list[str]:
    return [
        "\n".join(
            [
                "                    .       *       .",
                "             .                 .          *",
                "        ~                                 ~",
                "     ~~~~~~~                         ~~~~~~~",
                "        ~                                 ~",
            ]
        ),
        "\n".join(
            [
                "                 .     *     .      *",
                "            .              *           .",
                "        ~~      ~       ~       ~      ~~",
                "     ~~~~~~~~       · · · ·       ~~~~~~~~",
                "        ~~      ~       ~       ~      ~~",
            ]
        ),
        "\n".join(
            [
                "              *    .    *    .    *",
                "         .         ~~~~         .",
                "      ~~~~   ~~~~~~~~~~~~~~~   ~~~~",
                "     ~~~~~~~~   A U R O R A   ~~~~~~~~",
                "      ~~~~   ~~~~~~~~~~~~~~~   ~~~~",
            ]
        ),
        "\n".join(
            [
                "           .  *  .  *  .  *  .  *  .",
                "        ~     ~~~~~~~~~~~~~     ~",
                "     ~~~~~~ ~~~~~~~~~~~~~~~ ~~~~~~",
                "    ~~~~~~~~~  A U R O R A  ~~~~~~~~~",
                "     ~~~~~~ ~~~~~~~~~~~~~~~ ~~~~~~",
            ]
        ),
    ]


def animate_aurora_while(target: Callable[[], None]) -> BaseException | None:
    """Roda target numa thread; anima aurora até terminar. Retorna erro ou None."""
    box: dict[str, BaseException | None] = {"error": None}

    def worker() -> None:
        try:
            target()
        except BaseException as err:  # noqa: BLE001 — UI captura e exibe
            if isinstance(err, (KeyboardInterrupt, SystemExit)):
                box["error"] = err
                return
            box["error"] = err

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    frames = _aurora_frames()
    i = 0
    while thread.is_alive():
        if sys.stdout.isatty() and not NO_ANIM:
            sys.stdout.write("\033[2J\033[H")
        write(c("  ╔══════════════════════════════════════════════════════╗", MAGENTA, BOLD))
        write(c("  ║                    FASE 03: AURORA                  ║", MAGENTA, BOLD))
        write(c("  ╚══════════════════════════════════════════════════════╝", MAGENTA, BOLD))
        write()
        write(c('  "O silêncio precede o amanhecer."', DIM))
        write()
        write(c("  Estado: aguardando confirmação do delete OLD...", YELLOW))
        write()
        for line in frames[i % len(frames)].splitlines():
            write(c("  " + line, CYAN if i % 2 == 0 else BLUE))
        write()
        write(c("  A operação ainda está em andamento...", DIM))
        i += 1
        if NO_ANIM:
            thread.join()
            break
        sleep(0.35)
    thread.join()
    err = box["error"]
    if isinstance(err, (KeyboardInterrupt, SystemExit)):
        raise err
    return err


def victory_screen(held_old: bool = False) -> None:
    clear_screen()
    write(c("  ╔══════════════════════════════════════════════════════╗", GREEN, BOLD))
    write(c("  ║                                                      ║", GREEN, BOLD))
    write(c("  ║                  OPERAÇÃO AURORA                    ║", GREEN, BOLD))
    write(c("  ║                                                      ║", GREEN, BOLD))
    write(c("  ║              A AURORA CHEGOU                         ║", CYAN, BOLD))
    write(c("  ║                                                      ║", GREEN, BOLD))
    if held_old:
        write(
            c(
                "  ║   Refresh ok. OLD preservado (KEEP_OLD_DEV_HOURS). ║",
                YELLOW,
            )
        )
    else:
        write(
            c(
                "  ║   Clone/cutover ok. Vestígios (OLD) tratados.      ║",
                GREEN,
            )
        )
    write(c("  ║                                                      ║", GREEN, BOLD))
    write(c("  ║                  MISSÃO CONCLUÍDA                   ║", GREEN, BOLD))
    write(c("  ║                                                      ║", GREEN, BOLD))
    write(c("  ╚══════════════════════════════════════════════════════╝", GREEN, BOLD))
    write()
    pause("  [ Enter = sair ] ")


# ---------------------------------------------------------------------------
# Fail / hack
# ---------------------------------------------------------------------------

_AWS_CREDENTIAL_FIX_HINTS = [
    "Confira AWS_ACCESS_KEY_ID e AWS_SECRET_ACCESS_KEY no .env (sem aspas, sem espaço no fim).",
    "Se a chave foi rotacionada no IAM, use o Secret da chave ativa (só aparece na criação).",
    "Salve o .env e use [r] retry — o jogo recarrega variáveis sem reiniciar o container.",
    "Teste: make PYTHON=python account-aws",
]


def _format_stage_error(error: BaseException) -> tuple[str, list[str] | None]:
    """Mensagem curta para a UI; hints extras quando for erro de credencial AWS."""
    if isinstance(error, ClientError):
        code = (error.response.get("Error") or {}).get("Code") or ""
        if code == "SignatureDoesNotMatch":
            return (
                "Credenciais AWS inválidas: o Secret Access Key não confere com o "
                "Access Key ID (valor errado, truncado ou com aspas/espaço extra no .env).",
                list(_AWS_CREDENTIAL_FIX_HINTS),
            )
        if code in ("InvalidClientTokenId", "InvalidAccessKeyId"):
            return (
                "Access Key ID inválido, inexistente ou desativado no IAM.",
                list(_AWS_CREDENTIAL_FIX_HINTS),
            )
        if code == "UnrecognizedClientException":
            return (
                "Credenciais AWS não reconhecidas — confira Access Key e região.",
                list(_AWS_CREDENTIAL_FIX_HINTS),
            )
        if code == "ExpiredToken":
            return (
                "Token de sessão AWS expirado — renove credenciais temporárias e "
                "atualize AWS_SESSION_TOKEN no .env.",
                list(_AWS_CREDENTIAL_FIX_HINTS)
                + ["Credenciais temporárias exigem AWS_SESSION_TOKEN além de KEY/SECRET."],
            )
        if code in ("AccessDenied", "AccessDeniedException"):
            return (
                f"STS negou a chamada ({code}): credenciais válidas, mas sem permissão "
                "sts:GetCallerIdentity.",
                None,
            )
    return str(error), None


def fail_screen(
    stage: Stage,
    error: BaseException,
    cleared: list[str],
) -> str:
    clear_screen()
    write(c(" ╔══════════════════════════════════════════════╗", RED, BOLD))
    write(c(" ║           FALHA — etapa interrompida         ║", RED, BOLD))
    write(c(" ╚══════════════════════════════════════════════╝", RED, BOLD))
    write()
    meta = ACT_META[stage.act]
    write(c(f"  Fase: [{meta['num']}] {meta['title']} · {stage.title}", YELLOW))
    message, extra_hints = _format_stage_error(error)
    write(c("  Erro: ", RED, BOLD) + message)
    write()
    write(c("  O que fazer:", WHITE, BOLD))
    hints = extra_hints if extra_hints is not None else stage.hints
    for hint in hints:
        write(c(f"    • {hint}", WHITE))
    write()
    last = cleared[-1] if cleared else None
    if last:
        write(c(f"  Checkpoint salvo: {last}", CYAN))
    else:
        write(c("  Checkpoint: (nenhum)", CYAN))
    write()
    write(c("  [r] retry esta etapa", GREEN))
    write(c("  [c] continue / voltar ao hub", CYAN))
    write(c("  [q] quit", DIM))
    write()
    while True:
        try:
            choice = input(c("  > ", BOLD)).strip().lower()
        except EOFError:
            return "q"
        if choice in ("r", "c", "q"):
            return choice
        write(c("  use r, c ou q", DIM))


def simulate_error_menu(stage: Stage) -> BaseException | None:
    errors = list(stage.sample_errors) or [f"(simulado) etapa {stage.id} falhou"]
    write()
    write(c("  Possíveis erros:", YELLOW, BOLD))
    for i, msg in enumerate(errors, start=1):
        write(c(f"  [{i}] {msg}", WHITE))
    write(c("  [x] cancelar", DIM))
    write()
    while True:
        try:
            choice = input(c("  erro > ", BOLD)).strip().lower()
        except EOFError:
            return None
        if choice in ("x", "q", ""):
            return None
        if choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(errors):
                return RuntimeError(errors[n - 1])
        write(c(f"  use 1–{len(errors)} ou x", DIM))


def hack_menu(stage: Stage) -> str:
    write()
    write(c("  ══ HACK MENU ══", MAGENTA, BOLD))
    write(c(f"  etapa: {stage.title} ({stage.id})", DIM))
    write(c("  [e] simular erro", RED))
    write(c("  [p] passar etapa", GREEN))
    write(c("  [b] voltar etapa anterior", CYAN))
    write(c("  [x] voltar", DIM))
    write()
    while True:
        try:
            choice = input(c("  hack > ", BOLD)).strip().lower()
        except EOFError:
            return "x"
        if choice in ("e", "p", "b", "x"):
            return choice
        write(c("  use e, p, b ou x", DIM))


def prompt_check_or_hack() -> str:
    write(c("  [Enter] executar   [h] hack", DIM))
    while True:
        try:
            choice = input(c("  > ", BOLD)).strip().lower()
        except EOFError:
            return ""
        if choice in ("", "c", "check"):
            return ""
        if choice == "h":
            return "h"
        write(c("  Enter ou h", DIM))


def confirm_mutation(stage: Stage, app: AuroraClone) -> bool:
    """Confirmação explícita antes de mutar. Digite 'iniciar'."""
    write()
    write(c("  ── CONFIRMAÇÃO DE MUTAÇÃO ──", YELLOW, BOLD))
    write(c(f"  Etapa: {stage.confirm_label or stage.id}", WHITE))
    _show_targets(app)
    write()
    write(c("  Digite 'iniciar' para executar (ou Enter vazio = cancelar).", DIM))
    try:
        answer = input(c("  confirmar > ", BOLD)).strip().lower()
    except EOFError:
        return False
    return answer == "iniciar"


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------


def _quiet_logging() -> None:
    logging.getLogger().setLevel(logging.WARNING)


def _ops_logging() -> None:
    """Durante mutações, mostrar INFO do AuroraClone."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )


def _mark_cleared(cleared: list[str], stage_id: str) -> list[str]:
    existing = set(cleared)
    idx = stage_index(stage_id)
    for s in STAGES[: idx + 1]:
        existing.add(s.id)
    return [s.id for s in STAGES if s.id in existing]


def _draw_stage_header(stage: Stage, cleared: list[str]) -> None:
    meta = ACT_META[stage.act]
    banner = meta["banner"]
    write(c("  ╔══════════════════════════════════════════════════════╗", banner))
    write(
        c(
            f"  ║  FASE {meta['num']}: {meta['title']:<42}║",
            banner,
            BOLD,
        )
    )
    write(c("  ╚══════════════════════════════════════════════════════╝", banner))
    write(c(f'  "{meta["phrase"]}"', DIM))
    write()
    idx = stage_index(stage.id)
    write(
        c(
            f"  ── Etapa {idx + 1}/{len(STAGES)} · {stage.title} ──",
            WHITE,
            BOLD,
        )
    )
    write(c(f"  {stage.mission}", WHITE))
    if stage.mutates:
        write(c("  [!] Esta etapa MUTA recursos AWS/RDS.", YELLOW, BOLD))
    write()


def run_stage(
    stage: Stage,
    cleared: list[str],
    hack_on: bool,
    app: AuroraClone | None,
) -> tuple[str, list[str], AuroraClone | None, bool]:
    """
    Executa uma etapa. Retorna (outcome, cleared, app, held_old).
    outcome: ok | quit | hub
    held_old: True se Aurora rodou com KEEP_OLD > 0
    """
    clear_screen()
    _draw_stage_header(stage, cleared)
    held_old = False

    while True:
        if hack_on:
            while True:
                if prompt_check_or_hack() != "h":
                    break
                hack_choice = hack_menu(stage)
                if hack_choice == "x":
                    continue
                if hack_choice == "e":
                    simulated = simulate_error_menu(stage)
                    if simulated is None:
                        continue
                    choice = fail_screen(stage, simulated, cleared)
                    if choice == "q":
                        return "quit", cleared, None, False
                    if choice == "c":
                        return "hub", cleared, None, False
                    break
                if hack_choice == "p":
                    cleared = _mark_cleared(cleared, stage.id)
                    save_checkpoint(stage.id, cleared)
                    phase_clear(
                        stage.title,
                        clear_message=stage.clear_message or "(hack) etapa concluída",
                        act=active_act(set(cleared)),
                    )
                    pause("  [ Enter = hub ] ")
                    return "ok", cleared, None, False
                if hack_choice == "b":
                    idx = stage_index(stage.id)
                    if idx == 0:
                        write(c("  Já está na primeira etapa.", YELLOW))
                        pause("  [ Enter ] ")
                        continue
                    cleared = [s.id for s in STAGES[: idx - 1]]
                    cp_id = cleared[-1] if cleared else STAGES[0].id
                    save_checkpoint(cp_id, cleared)
                    write(c(f"  ← voltou (hub)", CYAN))
                    pause("  [ Enter = hub ] ")
                    return "hub", cleared, None, False

        try:
            _reload_dotenv()
            if stage.needs_app:
                _require_aws_env()
                app = AuroraClone()

            if stage.mutates:
                assert app is not None
                if not confirm_mutation(stage, app):
                    write(c("  Cancelado — voltando ao hub.", YELLOW))
                    pause("  [ Enter = hub ] ")
                    return "hub", cleared, app, False
                _ops_logging()

            write(c("  executando...", YELLOW))
            sleep(0.15)

            dynamic_msg: str | None
            if stage.id == "delete_old":
                assert app is not None
                held_old = app.keep_old_dev_hours > 0
                # Worker só muta; a UI anima no thread principal (sem corrida de stdout).
                run_error = animate_aurora_while(app.delete_old_dev)
                if run_error is not None:
                    raise run_error
                clear_screen()
                _draw_stage_header(stage, cleared)
                _show_targets(app)
                if held_old:
                    write(
                        c(
                            f"  [ OK ] Hold respeitado — OLD preservado "
                            f"(KEEP_OLD_DEV_HOURS={app.keep_old_dev_hours})",
                            YELLOW,
                        )
                    )
                    dynamic_msg = (
                        f"OLD preservado (KEEP_OLD_DEV_HOURS={app.keep_old_dev_hours})"
                    )
                else:
                    write(c("  [ OK ] OLD removido", GREEN))
                    dynamic_msg = f"OLD removido: {app.old_id}"
            else:
                dynamic_msg = stage.run(app)

        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            _quiet_logging()
            choice = fail_screen(stage, error, cleared)
            if choice == "q":
                return "quit", cleared, None, False
            if choice == "c":
                return "hub", cleared, None, False
            app = None
            clear_screen()
            _draw_stage_header(stage, cleared)
            continue

        _quiet_logging()
        cleared = _mark_cleared(cleared, stage.id)
        save_checkpoint(stage.id, cleared)
        phase_clear(
            stage.title,
            clear_message=dynamic_msg or stage.clear_message,
            act=active_act(set(cleared)),
        )
        pause("  [ Enter = hub ] ")
        return "ok", cleared, app, held_old


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def title_menu(cleared: list[str], hack_on: bool) -> tuple[str, list[str]]:
    """
    Tela inicial. Retorna (action, cleared).
    action: hub | quit
    """
    while True:
        clear_screen()
        title_banner()
        write(
            c(
                '  "Antes que a primeira luz toque o horizonte,',
                CYAN,
            )
        )
        write(c('   teremos atravessado as muralhas.', CYAN))
        write(
            c(
                '   O objetivo está à frente. A missão termina na Aurora."',
                CYAN,
            )
        )
        write(c("                                    — Algum modelo de IA", DIM))
        write()
        write(
            c(
                "  Missão: refresh Aurora DEV — Reconhecimento → Escalada → Aurora.",
                WHITE,
            )
        )
        write(
            c(
                "  Cada etapa só começa quando você der Enter no hub.",
                DIM,
            )
        )
        write(
            c(
                "  Mutações (!): pedem confirmação digitando 'iniciar'.",
                DIM,
            )
        )
        if hack_on:
            write(c("  HACK ON (AURORA_GAME_HACK)", MAGENTA, BOLD))
        write()
        if cleared:
            write(
                c(
                    f"  Progresso salvo: {len(cleared)}/{len(STAGES)} etapas concluídas.",
                    CYAN,
                )
            )
        else:
            write(c("  Progresso: nenhuma etapa concluída ainda.", DIM))
        write()
        write(c("  [Enter] abrir hub", GREEN))
        write(c("  [r]     resetar fases (desmarcar progresso)", YELLOW))
        write(c("  [q]     sair", DIM))
        write()
        try:
            choice = input(c("  > ", BOLD)).strip().lower()
        except EOFError:
            return "quit", cleared
        if choice in ("", "h", "hub"):
            return "hub", cleared
        if choice == "q":
            return "quit", cleared
        if choice == "r":
            if not cleared and not CHECKPOINT_PATH.is_file():
                write(c("  Nada para resetar.", DIM))
                pause("  [ Enter ] ")
                continue
            write(
                c(
                    "  Isso desmarca todas as etapas cleared e apaga o checkpoint.",
                    YELLOW,
                )
            )
            write(c("  Digite 'resetar' para confirmar (Enter vazio = cancelar).", DIM))
            try:
                confirm = input(c("  confirmar > ", BOLD)).strip().lower()
            except EOFError:
                continue
            if confirm != "resetar":
                write(c("  Reset cancelado.", DIM))
                pause("  [ Enter ] ")
                continue
            reset_checkpoint()
            cleared = []
            write(c("  Progresso resetado. Todas as etapas estão abertas do início.", GREEN))
            pause("  [ Enter ] ")
            continue
        write(c("  use Enter, r ou q", DIM))
        pause("  [ Enter ] ")


def run_game(start_id: str | None = None) -> int:
    require_tty()
    _quiet_logging()
    hack_on = _hack_enabled()

    cp = load_checkpoint()
    cleared: list[str] = list(cp.get("cleared") or [])

    prefer: str | None = None
    if start_id:
        if start_id not in STAGE_BY_ID and start_id != CLEAR_ID:
            write(c(f"Etapa desconhecida: {start_id}", RED))
            write("Ids: " + ", ".join(s.id for s in STAGES))
            return 2
        prefer = start_id
        if start_id in STAGE_BY_ID:
            cleared = [s.id for s in STAGES[: stage_index(start_id)]]
    elif cleared:
        prefer = default_cursor(set(cleared), hack_on)

    action, cleared = title_menu(cleared, hack_on)
    if action == "quit":
        write(c("  Operação Aurora abortada.", DIM))
        return 1
    # Após reset na tela inicial, cursor volta ao início.
    if not cleared:
        prefer = None

    cursor = default_cursor(set(cleared), hack_on, prefer=prefer)
    app: AuroraClone | None = None
    held_old = False

    while True:
        action, node = map_hub(set(cleared), cursor, hack_on)
        cursor = node
        if action == "quit":
            write(c("  Operação Aurora abortada.", DIM))
            return 1
        if action == "clear":
            victory_screen(held_old=held_old)
            return 0

        stage = STAGE_BY_ID[node]
        outcome, cleared, app, stage_held = run_stage(
            stage, cleared, hack_on, app
        )
        if stage.id == "delete_old" and outcome == "ok":
            held_old = stage_held
        if outcome == "quit":
            write(c("  Operação Aurora abortada.", DIM))
            return 1
        cursor = default_cursor(set(cleared), hack_on, prefer=node)
        app = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operação Aurora — campanha narrativa no terminal "
            "(avanço manual por etapa)"
        )
    )
    parser.add_argument(
        "--from",
        dest="from_phase",
        metavar="STAGE_ID",
        help="Abre o hub nesta etapa (anteriores marcadas cleared)",
    )
    parser.add_argument(
        "--no-anim",
        action="store_true",
        help="Pula sleeps das animações (debug)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    global NO_ANIM
    parser = build_parser()
    args = parser.parse_args(argv)
    NO_ANIM = bool(args.no_anim)
    code = run_game(start_id=args.from_phase)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
